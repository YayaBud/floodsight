"""Regression for FS-06/FS-07/FS-08/FS-14/FS-21: canonical geometry manifests.

hydraulic_ready must come from validate_geometry()'s real verdict, never a
hardcoded literal, and must differ per scenario based on what the DEM+OSM
data actually supports -- not be uniformly False or uniformly True.
"""
from __future__ import annotations

import json

import pytest
import rasterio

from src.m2_geometry.dem_utils import condition_dem
from src.m2_geometry.validation import (GeometryValidationError,
                                        ImpoundmentDoesNotHoldError,
                                        validate_geometry)
from src.scenarios import get_scenario_manifest

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
GEOMETRY_DIR = ROOT / "data" / "geometry"


def test_hydraulic_ready_is_not_hardcoded():
    """FS-21: at least one scenario must validate True and at least one must
    validate False with a real DEM-derived reason -- proving the flag is
    read from validate_geometry(), not a literal."""
    results = {k: get_scenario_manifest(k)["availability"]["hydraulic_ready"]
              for k in ("annamayya", "rishiganga", "phutkal", "south_lhonak")}
    assert True in results.values(), "no scenario validated -- suspicious for a live DEM+OSM check"
    assert False in results.values(), "every scenario validated -- suspicious, hydraulic_ready may be hardcoded True now"


@pytest.mark.parametrize("scenario_key,dem_key", [
    ("rishiganga", "rishiganga"),
    ("phutkal", "phutkal"),
])
def test_valid_geometry_manifest_passes_live_validation(scenario_key, dem_key):
    """Structurally valid, but the impoundment does not hold at DEM resolution.

    Originally: the cached `validated` flag must agree with a fresh run of
    validate_geometry(). It still catches a stale cache; what changed is the
    verdict, and the numbers below are the reason."""
    manifest = json.loads((GEOMETRY_DIR / f"{scenario_key}.json").read_text(encoding="utf-8"))
    with rasterio.open(ROOT / "data" / "dem" / f"{dem_key}_dem.tif") as ds:
        raw = ds.read(1); transform = ds.transform; crs = ds.crs; nodata = ds.nodata
    dem, _ = condition_dem(raw, nodata=nodata)
    result = validate_geometry(manifest, dem, transform, crs)
    assert result["valid"] is True
    assert manifest["validated"] is True

    # P2 (2026-09-12): structural validity is not retention. `validate_geometry`
    # now also MEASURES the level at which water escapes the upstream basin,
    # on the terrain with the barrier emplaced and with the face-averaged bed
    # the solver integrates. Both of these scenarios are configured above it:
    #
    #   rishiganga  escapes 2162.89 m against a 2175.68 m level -> 12.79 m over
    #   phutkal     escapes 3779.07 m against a 3794.11 m level -> 15.04 m over
    #
    # The barrier is continuous -- a cell-max escape search returns the crest
    # exactly for both. It is the LEVEL that is wrong, and that is what the
    # audit's E5 experiment was actually seeing when the Q=0 pool spread from
    # 35 to 195 wet cells. Reported as validity gate G4, not raised here.
    assert result["spill_level_m"] < manifest["water_level_m"], (
        "if a scenario's basin ever does hold its configured level, update the "
        "numbers above rather than deleting this assertion"
    )


def test_annamayya_geometry_honestly_fails_rather_than_fabricating():
    """FS-06/FS-14: annamayya's DEM does not resolve the embankment (recorded
    in memory.md) -- the geometry manifest must say so explicitly rather
    than shipping a fabricated river/seed pair to force a pass.

    Updated 2026-09-13. This used to assert `"river" not in roles`, on the
    basis that no OSM river cleared the barrier for annamayya. That was true of
    the DEM then on disk, which covered only 26.8 km of a 56.8 km AOI. After the
    DEM was refetched over the full bbox and `generate_geometry_manifest.py`
    learned to clip the OSM line to DEM coverage, the chain resolves and the
    role is populated from real data: classification OBSERVED, sourced to
    `data/admin/annamayya_rivers.geojson` (OSM way 165066847, "Mandavi River"),
    175 vertices, unbridged. Asserting its ABSENCE now pins an obsolete state.

    What the test is actually for -- annamayya must fail honestly rather than
    manufacture geometry to force a pass -- is unchanged and is what is asserted
    below: it still does not validate, it still says why, and any river it does
    carry must be traceable to the OSM extract rather than invented.
    """
    manifest = json.loads((GEOMETRY_DIR / "annamayya.json").read_text(encoding="utf-8"))
    assert manifest["validated"] is False
    assert manifest["validation_reason"]
    by_role = {f["properties"]["role"]: f["properties"]
               for f in manifest["geometry"]["features"]}
    river = by_role.get("river")
    if river is not None:
        assert river["classification"] in {"OBSERVED", "MODEL_RECONSTRUCTION"}
        assert ("openstreetmap" in river["source"].lower()
                or "data/admin" in river["source"]), (
            "annamayya's river must be traceable to the OSM extract, not invented")
    for seed in ("upstream_seed", "downstream_seed"):
        if seed in by_role:
            assert "river" in by_role[seed]["source"].lower(), (
                f"{seed} must be walked along a real river line")


def test_geometry_features_all_carry_role_classification_source():
    for path in GEOMETRY_DIR.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        for feature in data["geometry"]["features"]:
            props = feature["properties"]
            assert props.get("role"), f"{path.name}: feature missing role"
            assert props.get("classification"), f"{path.name}: feature missing classification"
            assert props.get("source"), f"{path.name}: feature missing source"
            assert props["classification"] != "OBSERVED" or "openstreetmap" in props["source"].lower() or "data/admin" in props["source"], (
                f"{path.name}: role {props['role']} claims OBSERVED without a traceable source"
            )
