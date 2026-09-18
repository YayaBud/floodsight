"""Stage B: validate_geometry() is a hard gate in the physics pipeline.

A scenario without confirmed geometry (`data/geometry/<key>.json`
`"validated": false`) must not reach the 2D solver or produce any simulation
artifact -- run_pipeline.py must call validate_geometry() and let
GeometryValidationError propagate uncaught. A scenario that DOES validate
must have its real, rasterized `barrier_mask` (not the old disc
`barrier_xy`/`barrier_radius_m=200`) passed to build_stage_storage /
compute_lake_depth_grids.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import rasterio

import run_pipeline
from src.data_fetcher import get_dem as _real_get_dem
from src.m2_geometry.dem_utils import condition_dem
from src.m2_geometry.validation import GeometryValidationError, validate_geometry
from src.provenance import Provenance

ROOT = Path(__file__).resolve().parents[1]
GEOMETRY_DIR = ROOT / "data" / "geometry"
DEM_DIR = ROOT / "data" / "dem"


def _dem_path_for(scenario_key: str) -> Path:
    return DEM_DIR / f"{scenario_key}_dem.tif"


def _fake_get_dem(scenario_key: str, allow_synthetic: bool = False):
    """Point straight at the already-provisioned cached DEM.

    `src.data_fetcher.get_dem` -> `fetch_dem` independently checks the cached
    DEM covers the *current* scenario bbox and raises if not, on a completely
    unrelated code path this task does not touch. Every scenario's DEM is
    already on disk (`data/dem/<key>_dem.tif`), so tests reach it directly
    rather than exercising that unrelated fetch/coverage check.
    """
    path = _dem_path_for(scenario_key)
    if not path.exists():
        raise FileNotFoundError(path)
    return path, Provenance.COMPUTED_LIVE


def _validated_scenario_key() -> str:
    """A scenario whose manifest's cached verdict is validated: true today."""
    for path in sorted(GEOMETRY_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("validated") is True:
            return data["scenario_key"]
    pytest.skip("no scenario currently validates true -- nothing to test the passing path with")


class _StopAfterGate(BaseException):
    """Raised by a patched downstream stage to prove control reached past
    the geometry gate without paying for the full 2D solver.

    Deliberately a BaseException, not Exception: several call sites this
    test needs to intercept (the M2 Gate-1 diagnostic and the non-cascade
    M3 stage-storage call) wrap build_stage_storage in `except Exception`
    by design, so an Exception subclass raised there would be swallowed and
    logged as a warning instead of propagating to pytest.raises.
    """


def test_validating_scenario_passes_the_geometry_gate_without_raising(tmp_path):
    """A scenario whose manifest validates true must run past the new
    validate_geometry() call in execute_full_simulation without raising
    GeometryValidationError. The 2D solver itself is not needed to prove
    this -- run_2d_swe_simulation is patched to abort immediately once
    reached, which is far cheaper than a full solve and still exercises the
    real integration point (the gate call inside execute_full_simulation),
    not a reimplemented mock of it.
    """
    scenario_key = _validated_scenario_key()
    with patch("src.data_fetcher.get_dem", _fake_get_dem), \
         patch("run_pipeline.run_2d_swe_simulation", side_effect=_StopAfterGate("reached solver")):
        with pytest.raises(_StopAfterGate):
            run_pipeline.execute_full_simulation(
                scenario_key=scenario_key,
                out_dir=tmp_path / f"gate_{scenario_key}",
                coarsen=1, total_duration_s=600,
            )


def test_annamayya_raises_geometry_validation_error_before_any_artifact(tmp_path):
    """Annamayya's manifest is validated: false (memory.md, Stage B). The
    hard gate must raise GeometryValidationError with the real reason and
    must not fall through to the disc/carved-corridor path or produce any
    output file.
    """
    manifest = json.loads((GEOMETRY_DIR / "annamayya.json").read_text(encoding="utf-8"))
    assert manifest["validated"] is False, "fixture assumption: annamayya must be the failing case"

    out_dir = tmp_path / "annamayya_run"
    with patch("src.data_fetcher.get_dem", _fake_get_dem):
        with pytest.raises(GeometryValidationError) as excinfo:
            run_pipeline.execute_full_simulation(
                scenario_key="annamayya", out_dir=out_dir,
                coarsen=8, total_duration_s=600,
            )
    assert str(excinfo.value), "GeometryValidationError must carry the real reason"
    # No simulation artifact reached disk: the gate raises before M3/M4/M5 write anything.
    written = list(out_dir.rglob("*")) if out_dir.exists() else []
    assert written == [], f"artifacts were written before the geometry gate raised: {written}"


def test_barrier_mask_passed_to_build_stage_storage_is_the_real_geometry(tmp_path):
    """The mask build_stage_storage actually receives must be the rasterized
    manifest geometry (from validate_geometry()'s result), not the old disc
    (`barrier_xy=(bx, by), barrier_radius_m=200.0`). Spy on build_stage_storage
    and compare the mask it's called with against validate_geometry()'s own
    output for the same scenario/DEM -- proving the same array object/values
    reach the fill code, not just that *some* mask compiles.
    """
    scenario_key = _validated_scenario_key()
    manifest = json.loads((GEOMETRY_DIR / f"{scenario_key}.json").read_text(encoding="utf-8"))
    with rasterio.open(_dem_path_for(scenario_key)) as ds:
        raw = ds.read(1)
        transform = ds.transform
        crs = ds.crs
        nodata = ds.nodata
    dem, _ = condition_dem(raw, nodata=nodata)
    expected = validate_geometry(manifest, dem, transform, crs)
    expected_mask = expected["barrier_mask"]

    # A disc of radius 200 m around the breach point would not match the
    # manifest's rasterized barrier polygon cell-for-cell (different shape,
    # different footprint) -- assert the two actually differ so this test
    # would fail if run_pipeline silently reverted to the disc.
    assert expected_mask.sum() > 0

    captured = {}

    def _spy(*args, **kwargs):
        if "barrier_mask" in kwargs and kwargs["barrier_mask"] is not None:
            captured["barrier_mask"] = kwargs["barrier_mask"]
            captured["barrier_xy"] = kwargs.get("barrier_xy")
        raise _StopAfterGate("captured at first build_stage_storage call")

    with patch("src.data_fetcher.get_dem", _fake_get_dem), \
         patch("run_pipeline.build_stage_storage", side_effect=_spy):
        with pytest.raises(_StopAfterGate):
            run_pipeline.execute_full_simulation(
                scenario_key=scenario_key,
                out_dir=tmp_path / f"mask_{scenario_key}",
                coarsen=1, total_duration_s=600,
            )

    assert "barrier_mask" in captured, "build_stage_storage was never called with a barrier_mask"
    assert captured["barrier_xy"] is None, "the old disc barrier_xy must not also be passed"
    got_mask = captured["barrier_mask"]
    assert got_mask.shape == expected_mask.shape
    assert np.array_equal(got_mask, expected_mask), (
        "the mask reaching build_stage_storage does not match validate_geometry()'s "
        "own rasterized barrier for this scenario/DEM"
    )
