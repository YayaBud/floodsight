"""Adversarial checks on the artifacts `scripts/backfill_visual_layers.py` writes.

Reads the produced files rather than re-running the backfill (which needs the
DEM, road graph and depth rasters and takes several minutes) — these are
regression checks on what is already on disk in
`data/scenarios/annamayya_compound/`. Skipped with a clear reason if that run
directory, or one of its artifacts, is absent.
"""
import json
import math
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "data" / "scenarios" / "annamayya_compound"

pytestmark = pytest.mark.skipif(
    not (RUN_DIR / "manifest.json").is_file(),
    reason=f"run directory not present: {RUN_DIR}")


def _load(name: str) -> dict:
    path = RUN_DIR / name
    if not path.is_file():
        pytest.skip(f"{name} not found in {RUN_DIR} — run backfill_visual_layers.py first")
    return json.loads(path.read_text(encoding="utf-8"))


def _first_raster_t_s() -> float:
    si = json.loads((ROOT / "data" / "scenarios" / "annamayya_stage2_full"
                     / "snapshots_index.json").read_text(encoding="utf-8"))
    return float(si[0]["t_s"])


# ── roads timeline ──────────────────────────────────────────────────────────

def test_roads_timeline_cut_times_and_count():
    data = _load("roads_timeline.geojson")
    feats = data["features"]
    assert len(feats) > 0

    first_t_s = _first_raster_t_s()
    for f in feats:
        ct_min = f["properties"]["cut_time_min"]
        if ct_min is not None:
            assert ct_min * 60.0 >= first_t_s - 1e-6, (
                f"edge cut at {ct_min} min, before the first raster time "
                f"{first_t_s / 60.0:.2f} min")

    # Feature count must equal what the module promises: one feature per edge
    # of the projected road graph it was given, not a magic number.
    import osmnx as ox
    from src.m6_isolation.isolation import edge_midpoints
    G = ox.load_graphml(ROOT / "data" / "roads" / "annamayya_roads.graphml")
    G_proj = ox.project_graph(G, to_crs="EPSG:32644")
    edge_keys, *_ = edge_midpoints(G_proj)
    assert len(feats) == len(edge_keys)


# ── evac routes ──────────────────────────────────────────────────────────────

def test_evac_routes_destinations_are_safe_and_fields_sane():
    import numpy as np
    import rasterio
    from pyproj import Transformer

    from src.rasterutils import sample_raster
    from src.m6_isolation.isolation import THRESH_CAR

    data = _load("evac_routes.geojson")
    feats = data["features"]
    assert len(feats) > 0

    with rasterio.open(RUN_DIR / "max_depth.tif") as src:
        max_depth = src.read(1).astype(float)
        tr, crs, nodata = src.transform, src.crs, src.nodata
    if nodata is not None:
        max_depth = np.where(max_depth == nodata, 0.0, max_depth)
    to_raster = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform

    for f in feats:
        p = f["properties"]
        x, y = to_raster(p["dest_lon"], p["dest_lat"])
        depth = float(sample_raster(max_depth, tr, np.array([x]), np.array([y]))[0])
        assert depth < THRESH_CAR, (
            f"{p['village_id']} destination depth {depth} >= threshold {THRESH_CAR}")

        assert p["route_cut_min"] is None or p["route_cut_min"] >= 0
        # A route whose destination IS the origin (village's nearest road node
        # already never wetted) is a real, computed zero-length route — see
        # backfill_visual_layers.py::build_evac_routes. travel_min is then
        # legitimately 0, not > 0; distinguished by status_note.
        if p["status_note"] == "village's nearest road node is itself never wetted in this run":
            assert p["travel_min"] == 0.0
        else:
            assert p["travel_min"] > 0


# ── lake frames ──────────────────────────────────────────────────────────────

def test_lake_frames_levels_and_volumes_monotone_and_bounded():
    data = _load("lake_frames.geojson")
    feats = data["features"]
    meta = data["metadata"]
    floor_m = meta["floor_m"]
    v_crest_m3 = meta["v_crest_mcm"] * 1e6

    rising = sorted((f["properties"] for f in feats if f["properties"]["phase"] == "rising"),
                    key=lambda p: p["t_min"])
    draining = sorted((f["properties"] for f in feats if f["properties"]["phase"] == "draining"),
                      key=lambda p: p["t_min"])
    assert rising and draining

    for a, b in zip(rising[:-1], rising[1:]):
        assert b["level_m"] >= a["level_m"] - 1e-9, "rising phase must be non-decreasing"
    for a, b in zip(draining[:-1], draining[1:]):
        assert b["level_m"] <= a["level_m"] + 1e-9, "draining phase must be non-increasing"

    for p in rising + draining:
        assert p["level_m"] >= floor_m - 1e-6
        assert p["volume_mcm"] * 1e6 <= v_crest_m3 + 1e-6


# ── water planes ─────────────────────────────────────────────────────────────

def test_water_planes_area_floor():
    data = _load("water_planes.geojson")
    feats = data["features"]
    assert len(feats) > 0
    for f in feats:
        assert f["properties"]["area_km2"] >= 1.0


# ── front field ──────────────────────────────────────────────────────────────

def test_front_field_unit_vectors():
    data = _load("front_field.json")
    u, v = data["u"], data["v"]
    assert len(u) == data["ny"] and len(u[0]) == data["nx"]
    checked = 0
    for row_u, row_v in zip(u, v):
        for uu, vv in zip(row_u, row_v):
            if uu is None or vv is None:
                continue
            mag = math.hypot(uu, vv)
            assert abs(mag - 1.0) < 1e-3, f"unit vector magnitude {mag} not within 1e-3 of 1"
            checked += 1
    assert checked > 0


# ── village depth ────────────────────────────────────────────────────────────

def test_village_depth_nonneg_and_times_strictly_increasing():
    data = _load("village_depth.json")
    village_entries = {k: v for k, v in data.items() if k != "provenance"}
    assert len(village_entries) > 0
    for vid, entry in village_entries.items():
        series = entry["series"]
        assert len(series) > 0
        times = [t for t, _ in series]
        depths = [d for _, d in series]
        assert all(d >= 0 for d in depths), f"{vid} has a negative depth"
        assert all(b > a for a, b in zip(times[:-1], times[1:])), (
            f"{vid} series times not strictly increasing: {times[:5]}...")
