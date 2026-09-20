"""The seed-separation refusal must carry the numbers that explain it.

"upstream/downstream seeds lack separated connected components" covers two
distinct causes -- a seed sitting above `water_level_m`, and a barrier that
fails to cut the wet region -- and the string tells them apart for nobody. The
2026-09-19 sweep spent a whole session re-deriving, by hand, numbers that
`validate_geometry` had already computed and discarded.

These tests pin two things: the message stays exactly the message (callers that
only read `str(exc)` must not see a tuple), and the discriminating numbers
travel with the refusal.

The most load-bearing field is `barrier_ends_above_level`. A barrier whose ends
stand above the water level has real abutments, so a failure is a terrain fact;
one with an end below the level is not spanning its valley at all. Extending
such a barrier until the labels separate is REJECTED work -- see
`scripts/generate_geometry_manifest.py` (its `_ridge_polygon` docstring) and
`findings_results.md` 2026-09-19, which measured the end elevations that make it
wrong. This test exists partly so that rejection keeps its evidence.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import rasterio
from rasterio import Affine

from src.m2_geometry.dem_utils import condition_dem
from src.m2_geometry.validation import GeometryValidationError, validate_geometry

ROOT = Path(__file__).resolve().parents[1]
GEOMETRY_DIR = ROOT / "data" / "geometry"
DEM_DIR = ROOT / "data" / "dem"

# Every manifest whose barrier is known not to cut its valley. All three are the
# same 404 m / 40,000 m2 rectangle that `_RIDGE_WINDOW_M = 200.0` produces.
NON_SPANNING = ("annamayya", "ivanovo", "malpasset")
COARSEN = 2


def _load(key: str):
    manifest_path = GEOMETRY_DIR / f"{key}.json"
    dem_path = DEM_DIR / f"{key}_dem.tif"
    if not manifest_path.exists() or not dem_path.exists():
        pytest.skip(f"{key}: manifest or DEM not on disk")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with rasterio.open(dem_path) as src:
        raw, transform, crs, nodata = src.read(1), src.transform, src.crs, src.nodata
    elevation, _ = condition_dem(raw, nodata=nodata)
    elevation = elevation[::COARSEN, ::COARSEN]
    transform = transform * Affine.scale(COARSEN, COARSEN)
    return manifest, elevation, transform, crs


@pytest.mark.parametrize("key", NON_SPANNING)
def test_message_is_still_only_the_message(key):
    """Passing a result dict must not leak into str(exc) as a tuple."""
    manifest, elevation, transform, crs = _load(key)
    with pytest.raises(GeometryValidationError) as excinfo:
        validate_geometry(manifest, elevation, transform, crs)
    assert str(excinfo.value) == (
        "upstream/downstream seeds lack separated connected components")


@pytest.mark.parametrize("key", NON_SPANNING)
def test_refusal_carries_the_discriminating_numbers(key):
    manifest, elevation, transform, crs = _load(key)
    with pytest.raises(GeometryValidationError) as excinfo:
        validate_geometry(manifest, elevation, transform, crs)
    result = excinfo.value.result
    assert result, "refusal carried no diagnostics"

    assert result["cause"] == "barrier does not cut the wet region"
    # Same label for both seeds is what "does not cut" means; the shared
    # component must be the bulk of the wet region, not an incidental overlap.
    assert result["upstream_label"] == result["downstream_label"] != 0
    assert result["shared_component_cells"] > 0.5 * result["wet_cells_below_level"]

    # The discriminator. At least one end of the barrier is under its own water
    # level -- it is not spanning the valley, which is why growing it is wrong.
    ends = result["barrier_end_elev_m"]
    assert len(ends) == 2, "barrier principal axis did not resolve two ends"
    assert min(ends) < result["water_level_m"]
    assert result["barrier_ends_above_level"] is False


def test_passing_geometry_does_not_raise():
    """Guard the other direction: the diagnostics must not turn a pass into a
    failure. phutkal's barrier does cut its gorge."""
    manifest, elevation, transform, crs = _load("phutkal")
    result = validate_geometry(manifest, elevation, transform, crs)
    assert result["valid"] is True
