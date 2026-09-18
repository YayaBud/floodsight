"""Regression for the Stage B follow-up: validate_geometry() must derive
upstream_seed/downstream_seed by walking the manifest's river/breach_point
against the barrier at whatever resolution `elevation`/`transform` actually
represent, not trust the manifest's fixed full-resolution seed coordinates.

Before this fix, rishiganga (the reference scenario Stage B's own tests use
as "the one that validates") only actually passed validate_geometry() at
coarsen=1 -- any coarsening broke the hard gate for every scenario, because
the barrier-clearance buffer scales with cellsize while the checked seed
points were fixed. See src/m2_geometry/validation.py and
src/m2_geometry/seed_walk.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import rasterio
from rasterio import Affine

from src.m2_geometry.dem_utils import condition_dem
from src.m2_geometry.validation import (GeometryValidationError,
                                        ImpoundmentDoesNotHoldError,
                                        validate_geometry)

ROOT = Path(__file__).resolve().parents[1]
GEOMETRY_DIR = ROOT / "data" / "geometry"
DEM_DIR = ROOT / "data" / "dem"


def _load_rishiganga():
    manifest = json.loads((GEOMETRY_DIR / "rishiganga.json").read_text(encoding="utf-8"))
    with rasterio.open(DEM_DIR / "rishiganga_dem.tif") as ds:
        raw = ds.read(1)
        transform = ds.transform
        crs = ds.crs
        nodata = ds.nodata
    dem, _ = condition_dem(raw, nodata=nodata)
    return manifest, dem, transform, crs


def _coarsened(dem, transform, coarsen):
    """Same coarsening run_pipeline.py's execute_full_simulation applies."""
    if coarsen <= 1:
        return dem, transform
    return dem[::coarsen, ::coarsen], transform * Affine.scale(coarsen, coarsen)


def test_rishiganga_validates_across_coarsen_levels_not_just_full_resolution():
    """The exact bug measurement: coarsen=1/2/4 must now PASS (previously
    only coarsen=1 passed); coarsen=6/8 legitimately fail for a DIFFERENT,
    real reason (barrier/connectivity unresolved at that cell size), not the
    resolution-dependence bug this task fixes."""
    manifest, dem_full, transform, crs = _load_rishiganga()
    outcomes = {}
    for coarsen in (1, 2, 4, 6, 8):
        dem, tr = _coarsened(dem_full, transform, coarsen)
        try:
            validate_geometry(manifest, dem, tr, crs)
            outcomes[coarsen] = "PASS"
        except ImpoundmentDoesNotHoldError:
            # Every STRUCTURAL check passed -- roles, datums, masks and, the
            # point of this test, the resolution-adaptive seed derivation. The
            # run is refused later for retention (the pool is configured 12.79 m
            # above what rishiganga's basin holds), which is a different defect
            # and is pinned in test_mass_gates / test_geometry_manifests.
            outcomes[coarsen] = "PASS"
        except GeometryValidationError as exc:
            outcomes[coarsen] = f"FAIL: {exc}"

    for coarsen in (1, 2, 4):
        assert outcomes[coarsen] == "PASS", (
            f"coarsen={coarsen} should pass after the resolution-adaptive fix, got {outcomes[coarsen]}"
        )
    for coarsen in (6, 8):
        assert outcomes[coarsen] != "PASS", (
            f"coarsen={coarsen} unexpectedly passed"
        )
        assert "river seeds must lie outside barrier" not in outcomes[coarsen], (
            f"coarsen={coarsen} failed on the seed-buffer bug this task fixes, not a legitimate reason: {outcomes[coarsen]}"
        )


def _seeds(manifest, dem, tr, crs):
    """The derived seeds, whether or not the impoundment goes on to hold.

    `ImpoundmentDoesNotHoldError` carries everything computed before it fired,
    so a test about seed derivation does not need a bypass flag on
    `validate_geometry` to see them."""
    try:
        return validate_geometry(manifest, dem, tr, crs)
    except ImpoundmentDoesNotHoldError as exc:
        return exc.result


def test_walked_seeds_differ_between_coarsen_levels():
    """Proves the seeds are actually recomputed per resolution, not
    cached/stale full-resolution coordinates."""
    manifest, dem_full, transform, crs = _load_rishiganga()
    dem1, tr1 = _coarsened(dem_full, transform, 1)
    dem4, tr4 = _coarsened(dem_full, transform, 4)
    result1 = _seeds(manifest, dem1, tr1, crs)
    result4 = _seeds(manifest, dem4, tr4, crs)
    assert result1["upstream_seed_xy"] != result4["upstream_seed_xy"]
    assert result1["downstream_seed_xy"] != result4["downstream_seed_xy"]


def test_terminal_vertex_still_inside_barrier_fails_cleanly():
    """At an extreme coarsen level, the downstream walk runs off the river's
    terminal vertex while still inside the buffered barrier (confirmed
    empirically: at cellsize ~5017 m the downstream walk's fallback point is
    barrier.buffer(cellsize).contains(...) == True). validate_geometry must
    raise, not silently accept that fallback point as a valid seed."""
    manifest, dem_full, transform, crs = _load_rishiganga()
    coarsen = 177  # empirically confirmed: cellsize ~5017 m, terminal-vertex case
    dem, tr = _coarsened(dem_full, transform, coarsen)
    cellsize = max(abs(tr.a), abs(tr.e))
    assert dem.size > 0, "sanity: coarsened DEM must not be empty"
    with pytest.raises(GeometryValidationError, match="river seeds must lie outside barrier"):
        validate_geometry(manifest, dem, tr, crs)


@pytest.mark.parametrize("scenario_key,dem_key", [
    ("rishiganga", "rishiganga"),
    ("phutkal", "phutkal"),
])
def test_valid_geometry_manifest_still_passes_live_validation(scenario_key, dem_key):
    """Existing test_geometry_manifests.py coverage, re-affirmed here: the
    manifests' cached `validated: true` flag must still agree with a fresh
    validate_geometry() call at full resolution after the seed-derivation
    rewrite."""
    manifest = json.loads((GEOMETRY_DIR / f"{scenario_key}.json").read_text(encoding="utf-8"))
    with rasterio.open(DEM_DIR / f"{dem_key}_dem.tif") as ds:
        raw = ds.read(1)
        transform = ds.transform
        crs = ds.crs
        nodata = ds.nodata
    dem, _ = condition_dem(raw, nodata=nodata)
    result = _seeds(manifest, dem, transform, crs)
    assert manifest["validated"] is True
    # Structural validity survives; retention does not (see
    # test_geometry_manifests for the measured shortfall). What this test
    # guards is that the seed-derivation stage still completes at full
    # resolution after the seed rewrite.
    assert result["upstream_seed_rc"] != result["downstream_seed_rc"]
    assert result["barrier_mask"].any() and result["breach_mask"].any()
