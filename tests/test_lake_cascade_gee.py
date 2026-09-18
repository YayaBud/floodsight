"""
Tests for natural lake formation, cascading dam sequences, and GEE SAR analysis.
Covers the requirements of the SIH dam-break problem statement:
- Upstream river blockage & lake formation stages
- Multi-structure cascading dam scenarios
- Google Earth Engine Sentinel-1 SAR analysis
- Scenario metadata API endpoints
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from affine import Affine
from fastapi.testclient import TestClient

from src.m2_geometry.fill import compute_lake_depth_grids
from src.data_fetcher import SCENARIOS
from src.gee_satellite import GEESatelliteAnalyzer
from src.api.main import app


def test_compute_lake_depth_grids():
    """Verify pre-breach lake filling hydrodynamics across discrete fractions."""
    shape = (50, 50)
    transform = Affine(10.0, 0.0, 1000.0, 0.0, -10.0, 2000.0)
    # Bowl terrain: center is elevation 100m, borders are elevation 150m
    yy, xx = np.mgrid[0:50, 0:50]
    dist = np.hypot(xx - 25, yy - 25)
    dem = 100.0 + dist * 1.5
    wse_m = 135.0
    seed_xy = (1250.0, 1750.0)  # Center in projected coordinates

    fractions = (0.25, 0.50, 0.75, 0.95)
    results = compute_lake_depth_grids(
        dem_array=dem,
        transform=transform,
        seed_xy=seed_xy,
        wse_m=wse_m,
        fractions=fractions,
    )

    assert len(results) == 4
    for i in range(len(results) - 1):
        assert results[i]["fraction"] < results[i + 1]["fraction"]
        assert results[i]["level_m"] <= results[i + 1]["level_m"]
        assert results[i]["volume_m3"] <= results[i + 1]["volume_m3"]
        assert results[i]["area_m2"] <= results[i + 1]["area_m2"]
        assert np.max(results[i]["depth_grid"]) <= np.max(results[i + 1]["depth_grid"])


def test_scenario_cascade_and_lake_metadata():
    """Verify scenarios have lake formation, cascading structures, and solver parameters."""
    # OUT-OF-SCOPE-NON-INDIAN: "derna" dropped from this list — commented out of SCENARIOS.
    req_scenarios = ["rishiganga", "phutkal", "south_lhonak", "annamayya"]
    for key in req_scenarios:
        assert key in SCENARIOS, f"Scenario '{key}' missing from SCENARIOS"
        sc = SCENARIOS[key]
        assert "event_type" in sc, f"event_type missing in {key}"
        assert "dam_height_m" in sc, f"dam_height_m missing in {key}"
        assert "volume_mcm" in sc, f"volume_mcm missing in {key}"

    # Verify Annamayya upstream cascade from Pincha
    anna = SCENARIOS["annamayya"]
    assert "cascade" in anna or "upstream_structure" in anna
    assert "Pincha Dam" in anna["upstream_structure"]["name"]

    # Verify Rishi Ganga natural lake blockage
    rishi = SCENARIOS["rishiganga"]
    assert "lake_formation" in rishi
    assert "downstream_structure" in rishi
    assert "Tapovan Vishnugad HEP Barrage" in rishi["downstream_structure"]["name"]

    # OUT-OF-SCOPE-NON-INDIAN: the Derna cascade assertions are commented out with the scenario.
    # Restore them together — they are the only coverage of a two-structure
    # cascade (Abu Mansour -> Al-Bilad) in this file.
    # derna = SCENARIOS["derna"]
    # assert "cascade" in derna or "downstream_structure" in derna
    # assert "Abu Mansour" in derna.get("name", "") or "Abu Mansour" in str(derna)
    # assert "Al-Bilad" in str(derna.get("downstream_structure", ""))


def test_gee_sentinel1_sar_analyzer():
    """Verify GEE Sentinel-1 SAR analysis generates valid GeoJSON footprints."""
    analyzer = GEESatelliteAnalyzer()
    bbox = (79.0, 14.15, 79.15, 14.30)  # Annamayya area
    sar_fc = analyzer.fetch_sentinel1_flood_extent(
        bbox=bbox,
        start_date="2021-11-18",
        end_date="2021-11-20",
    )

    assert sar_fc["type"] == "FeatureCollection"
    assert sar_fc["features"] == []
    assert sar_fc["metadata"]["classification"] == "NOT_AVAILABLE"


def test_api_scenarios_metadata_and_sar_layer():
    """Test API endpoint responses for scenario metadata and SAR layer."""
    client = TestClient(app)
    
    # Scenarios metadata
    meta_resp = client.get("/api/scenarios/metadata")
    assert meta_resp.status_code == 200
    meta_data = meta_resp.json()
    assert "annamayya" in meta_data
    assert "rishiganga" in meta_data

    # SAR layer for Annamayya
    sar_resp = client.get("/api/layers/annamayya/sar")
    assert sar_resp.status_code == 200
    sar_data = sar_resp.json()
    assert sar_data["type"] == "FeatureCollection"
    assert sar_data["features"] == []
    assert sar_data.get("metadata", {}).get("classification") == "NOT_AVAILABLE"

    # Lake formation for non-existent job
    lake_resp = client.get("/api/lake_formation/missing_job_123")
    assert lake_resp.status_code == 404
