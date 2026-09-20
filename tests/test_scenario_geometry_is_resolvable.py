"""Every scenario must resolve to geometry, or say why it cannot — not crash.

`run_pipeline.execute_full_simulation` reads
`data/geometry/<scenario_key>.json`. Before 2026-09-19 it read it with no
existence check, so `south_lhonak` died on `FileNotFoundError` and
`src/api/worker.py` surfaced that as a failed manifest whose only stated reason
was an OS errno and a Windows path.

south_lhonak is not missing geometry. It is a **compound event with two
structures**, so its geometry is authored per structure
(`south_lhonak_chungthang.json`, `south_lhonak_moraine.json`) and nothing is ever
written under the scenario key itself. The refusal now says that.

This test pins the data shape the guard reports, which is cheap; exercising the
guard itself would mean running the pipeline far enough to load a 16 MB DEM.

ponytail: data-shape assertion, not an end-to-end run. Promote to an
integration test only if the guard itself starts changing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.data_fetcher import SCENARIOS, scenario_key_for

ROOT = Path(__file__).resolve().parents[1]
GEOMETRY_DIR = ROOT / "data" / "geometry"


@pytest.mark.parametrize("scenario_key", sorted(SCENARIOS))
def test_scenario_has_geometry_under_its_own_key_or_per_structure(scenario_key):
    own = GEOMETRY_DIR / f"{scenario_key}.json"
    per_structure = sorted(GEOMETRY_DIR.glob(f"{scenario_key}_*.json"))
    assert own.exists() or per_structure, (
        f"{scenario_key} has no geometry manifest under its own key and no "
        f"per-structure manifests either — the pipeline cannot refuse it "
        f"informatively, it can only fail to find a file")


def test_south_lhonak_is_the_per_structure_case():
    """Guard the compound-event shape explicitly.

    If a `south_lhonak.json` ever appears, this fails on purpose: the event has
    two structures and collapsing them into one manifest would silently pick a
    single barrier for a two-barrier cascade.
    """
    assert not (GEOMETRY_DIR / "south_lhonak.json").exists(), (
        "south_lhonak.json appeared — the event has two structures and needs a "
        "compound-event path that selects between them, not one merged manifest")
    structures = sorted(p.stem for p in GEOMETRY_DIR.glob("south_lhonak_*.json"))
    assert structures == ["south_lhonak_chungthang", "south_lhonak_moraine"]


def test_south_lhonak_dem_is_present():
    """memory.md recorded 'no manifest under its own key and no DEM' on
    2026-09-12. The DEM half is STALE — it has been on disk since 2026-09-06."""
    assert (ROOT / "data" / "dem" / "south_lhonak_dem.tif").exists()


def test_scenario_key_for_is_identity_for_single_structure_events():
    """Every scenario key maps to itself — the mapping must not rewrite them."""
    for key in SCENARIOS:
        assert scenario_key_for(key) == key


def test_scenario_key_for_maps_south_lhonak_structures():
    assert scenario_key_for("south_lhonak_chungthang") == "south_lhonak"
    assert scenario_key_for("south_lhonak_moraine") == "south_lhonak"


def test_scenario_key_for_leaves_unmatched_keys_alone():
    """derna / ivanovo / malpasset have no SCENARIOS entry and must keep failing
    for that reason rather than being silently attached to some other scenario."""
    for key in ("derna", "ivanovo", "malpasset", "not_a_dam"):
        assert scenario_key_for(key) == key


def test_scenario_key_for_prefers_the_longest_prefix():
    """A shorter scenario key must not capture a longer one's structure.

    Guard for the day a `phutkal_*` structure is added alongside a hypothetical
    `phutkal_upper` scenario: the structure belongs to the more specific key.
    """
    import src.data_fetcher as df
    original = df.SCENARIOS
    try:
        df.SCENARIOS = {"phutkal": {}, "phutkal_upper": {}}
        assert df.scenario_key_for("phutkal_upper_east") == "phutkal_upper"
        assert df.scenario_key_for("phutkal_east") == "phutkal"
    finally:
        df.SCENARIOS = original
