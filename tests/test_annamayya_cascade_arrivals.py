"""
Unit tests for Annamayya cascade simulation, pre-breach rise, arrival validation, and SPH scenario profile.
"""
import json
import numpy as np
import pytest
import rasterio
from affine import Affine

from src.data_fetcher import SCENARIOS
from src.m3_breach.cascade import simulate_reservoir_cascade, ReservoirCascadeResult
from src.m10_validation.compare_arrivals import compare_arrivals, HISTORICAL_ARRIVALS
from src.m4_solvers.sph_swe import run_scenario_thalweg_sph


def test_simulate_prebreach_rise():
    """Pre-trigger portion (t_s <= 0) of the single continuous reservoir
    integration: starts near FRL and has risen toward/past crest by t=0."""
    cfg = SCENARIOS["annamayya"]["cascade"]

    res = simulate_reservoir_cascade(
        cfg=cfg,
        pre_breach_s=5400.0,
        dt_s=30.0,
    )

    assert isinstance(res, ReservoirCascadeResult)
    pre_mask = res.t_s <= 0.0
    pre_t_s = res.t_s[pre_mask]
    pre_elev = res.reservoir_elevation_m[pre_mask]

    assert len(pre_t_s) > 0
    assert len(pre_elev) == len(pre_t_s)
    # Initial elevation near FRL (+203.6m)
    assert np.isclose(pre_elev[0], 203.6, atol=0.2)
    # Elevation at t_s == 0 (or nearest sample) has risen toward/past crest.
    idx0 = int(np.argmin(np.abs(pre_t_s)))
    assert pre_elev[idx0] >= 204.0


def test_compare_arrivals(tmp_path):
    # Construct a synthetic geotiff raster in EPSG:4326 covering Cheyyeru corridor
    # 79.0 to 79.2 E, 14.2 to 14.4 N
    nx, ny = 100, 100
    transform = Affine.translation(79.0, 14.40) @ Affine.scale(0.2 / nx, -0.2 / ny)
    
    depth_arr = np.full((ny, nx), 3.5, dtype=np.float32)
    max_depth_tif = tmp_path / "max_depth.tif"
    
    with rasterio.open(
        max_depth_tif,
        "w",
        driver="GTiff",
        height=ny,
        width=nx,
        count=1,
        dtype=np.float32,
        crs="EPSG:4326",
        transform=transform,
    ) as dst:
        dst.write(depth_arr, 1)

    # Frame rasters for arrival time
    raster_stack = []
    for t_min in [15.0, 35.0, 45.0, 60.0, 90.0, 130.0]:
        t_s = t_min * 60.0
        frame_tif = tmp_path / f"depth_{int(t_s)}.tif"
        frame_arr = np.full((ny, nx), 1.5, dtype=np.float32)
        with rasterio.open(
            frame_tif,
            "w",
            driver="GTiff",
            height=ny,
            width=nx,
            count=1,
            dtype=np.float32,
            crs="EPSG:4326",
            transform=transform,
        ) as dst:
            dst.write(frame_arr, 1)
        raster_stack.append((t_s, frame_tif))

    res = compare_arrivals(
        scenario_key="annamayya",
        max_depth_tif=max_depth_tif,
        raster_stack=raster_stack,
        out_dir=tmp_path,
        arrival_threshold_m=0.30,
        search_radius_m=2000.0,
    )

    # FLIPPED 2026-09-14, and only because the thing it was waiting for now exists.
    # These assertions were CORRECT while `data/observations/annamayya/arrivals.json`
    # was absent: `_verified_arrivals` refuses anything without a source manifest,
    # so six OBSERVED records sat demoted and arrival validation reported
    # NOT_AVAILABLE. That manifest is now authored, by script, from the evidence
    # file's own `event_clock.timeline_events`
    # (scripts/author_arrival_manifest.py) with a SHA-256 of the evidence file on
    # the payload and of each event on its record. The gate was not weakened --
    # it was satisfied.
    assert res["available"] is True, (
        "arrival validation went back to NOT_AVAILABLE — the manifest at "
        "data/observations/annamayya/arrivals.json is missing or no longer "
        "passes _verified_arrivals")
    assert (tmp_path / "validation_arrivals.json").exists()
    assert res["total_points"] == 5, (
        "expected the 5 evaluable records from the manifest; the other 4 are "
        "pre-T=0 (pre-washout overtopping flow, which a run whose t=0 is the "
        "washout cannot model) and 1 has no sourced coordinate")
    # Every evaluated record must carry the provenance it was admitted under --
    # a record without its hash is not evidence.
    assert res.get("results"), "no per-location results returned"
    for loc in res["results"]:
        assert loc.get("id") and loc.get("name")
        assert len(loc["source_hash"]) == 64
        assert loc["observation_context"]["acquisition_proof"]["kind"] == "DOCUMENTARY"


def test_run_scenario_thalweg_sph():
    # Synthetic DEM and thalweg
    nx, ny = 50, 50
    dem = np.linspace(250, 150, ny * nx).reshape(ny, nx)
    transform = Affine.identity()
    
    # 10 km corridor
    thalweg = [(float(i * 200), float(i * 200)) for i in range(50)]
    t_hydro = np.array([0.0, 600.0, 1800.0, 3600.0])
    q_hydro = np.array([100.0, 5000.0, 3000.0, 500.0])
    
    comp = run_scenario_thalweg_sph(
        scenario_key="test_annamayya",
        dem_array=dem,
        transform=transform,
        thalweg_pts_utm=thalweg,
        hydrograph_t_s=t_hydro,
        hydrograph_q_m3s=q_hydro,
        channel_width_m=80.0,
        total_duration_s=60.0,
        n_stations=20,
    )
    
    assert comp["scenario"] == "test_annamayya"
    assert comp["available"] is False
    assert comp["status"] == "NOT_AVAILABLE"
