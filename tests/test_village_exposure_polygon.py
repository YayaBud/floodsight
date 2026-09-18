"""A village is flooded when its POLYGON floods, not when its centroid cell does.

`scripts/route_annamayya.py` sampled ONE cell — the polygon centroid — and called
that the whole village:

    c_ = r.geometry.centroid
    cc = int((c_.x - tr.c) / tr.a); rw = int((c_.y - tr.f) / tr.e)
    d_ = float(res.max_depth_grid[rw, cc])

At 152 m that is 2.32 ha standing in for a settlement kilometres across. Measured on
`annamayya_stage2_wide`: **11 of 23 settlements reported NOT REACHED while part of
their polygon was under water** — Lebaka 60 % flooded, Obili 44 % at 3.13 m, Gundlur
33 % at 3.11 m — and **30,571 people went unscored**.

Gundlur is the one that matters most: EVD-25 *reports* 2.0–4.0 m there. The model
produced 3.11 m over its polygon and the centroid sample threw it away, so a
reproduced observation looked like a miss.

`src/m5_exposure/exposure.py::compute_village_exposure` already did this correctly and
is what `run_pipeline` uses. The centroid version was a parallel reimplementation — the
exact "near-miss" failure INVARIANTS.md warns about.

Asserts on the real `village_exposure`, never on a restatement of its rule.
"""
from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
from affine import Affine
from shapely.geometry import box

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from route_annamayya import village_exposure  # noqa: E402


def _grids():
    """10x10 at 100 m. Rows 0-4 dry, rows 5-9 under 2 m, arriving 600 s later."""
    depth = np.zeros((10, 10), dtype=float)
    depth[5:, :] = 2.0
    arrival = np.full((10, 10), np.nan)
    arrival[5:, :] = 3600.0
    # One cell floods earlier, to prove the village reports the FIRST arrival
    # inside its polygon rather than whatever happens to sit under its centroid.
    arrival[9, 9] = 1200.0
    transform = Affine(100.0, 0, 0, 0, -100.0, 0)
    return depth, arrival, transform


def _village(minx, miny, maxx, maxy, pop=1000, name="V"):
    return gpd.GeoDataFrame(
        {"village_name": [name], "pop_total": [pop]},
        geometry=[box(minx, miny, maxx, maxy)], crs="EPSG:32644")


def test_half_flooded_village_is_not_reported_dry():
    """The regression. Centroid lands in the dry half; half the polygon is wet."""
    depth, arrival, tr = _grids()
    # y from -400 (row 4, dry) to -700 (row 7, wet); centroid row is 5... so bias
    # the box so its centroid sits in the DRY half while 40 % of it is wet.
    vil = _village(0, -600, 500, 0)          # rows 0..5, centroid at row 3 (dry)
    out = village_exposure(vil, depth, arrival, tr)

    assert out["max_depth_m"][0] == pytest.approx(2.0), out
    assert out["inundated"][0] is True
    assert out["flooded_area_frac"][0] > 0.0
    # and the centroid really was dry, so the old code would have said 0.0
    c = vil.geometry.iloc[0].centroid
    cc = int((c.x - tr.c) / tr.a); rw = int((c.y - tr.f) / tr.e)
    assert depth[rw, cc] == 0.0, "fixture no longer reproduces the bug"


def test_fully_dry_village_stays_dry():
    depth, arrival, tr = _grids()
    out = village_exposure(_village(0, -400, 400, 0), depth, arrival, tr)
    assert out["max_depth_m"][0] == 0.0
    assert out["inundated"][0] is False
    assert out["water_arrival_min"][0] is None
    assert out["pop_at_risk"][0] == 0


def test_population_is_apportioned_by_flooded_area_not_all_or_nothing():
    """Matches `compute_village_exposure`: pop_total * area_frac, PROXY."""
    depth, arrival, tr = _grids()
    vil = _village(0, -1000, 1000, 0, pop=1000)   # exactly half the rows wet
    out = village_exposure(vil, depth, arrival, tr)
    assert out["flooded_area_frac"][0] == pytest.approx(0.5)
    assert out["pop_at_risk"][0] == pytest.approx(500, abs=1)
    # A 3 %-flooded village must not contribute its whole population.
    small = _village(0, -600, 1000, 0, pop=1000)
    frac = village_exposure(small, depth, arrival, tr)["flooded_area_frac"][0]
    assert 0.0 < frac < 1.0


def test_arrival_is_the_earliest_wet_cell_in_the_polygon():
    depth, arrival, tr = _grids()
    vil = _village(0, -1000, 1000, 0)
    out = village_exposure(vil, depth, arrival, tr)
    # 1200 s = 20 min, the early cell at [9,9] — not the 60 min everywhere else.
    assert out["water_arrival_min"][0] == pytest.approx(20.0)


def test_village_outside_the_grid_is_a_status_not_a_silent_zero():
    depth, arrival, tr = _grids()
    out = village_exposure(_village(50_000, -51_000, 51_000, -50_000), depth, arrival, tr)
    assert out["max_depth_m"][0] == 0.0
    assert out["inundated"][0] is False
    assert out["exposure_status"][0] != "OK"
