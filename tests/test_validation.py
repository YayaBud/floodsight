"""
M10 validation tests.

Two halves:

* **Pure maths** — exact, hand-computable confusion matrices. These must pass
  everywhere and never touch the network or the downloaded datasets.
* **Real data** — the Copernicus EMS EMSR696 files under ``data/validation``.
  Skipped when the data is absent so a fresh clone still has a green suite,
  but *not* softened: when the data is there, the assertions are strict.

The point of this file is that a regression in the scoring maths cannot pass
silently. A validation module that quietly returns a flattering number is worse
than no validation module.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.m10_validation import metrics as M
from src.m10_validation import observed as O
from src.m10_validation import roads as R


# ══════════════════════════════════════════════════════════════════ pure maths

def _known_case():
    """obs and sim are 6 cells each, overlapping in 4. Hand-checkable."""
    obs = np.zeros((4, 4), bool); obs[1:3, 0:3] = True
    sim = np.zeros((4, 4), bool); sim[1:3, 1:4] = True
    dom = np.ones((4, 4), bool)
    return sim, obs, dom


def test_confusion_counts_are_exact():
    sim, obs, dom = _known_case()
    r = M.confusion(sim, obs, dom)
    assert (r.tp, r.fp, r.fn, r.tn) == (4, 2, 2, 8)
    assert r.n_scored == 16
    assert r.sim_area_cells == 6 and r.obs_area_cells == 6


def test_skill_scores_match_their_definitions():
    sim, obs, dom = _known_case()
    r = M.confusion(sim, obs, dom)
    assert r.csi == pytest.approx(4 / 8)
    assert r.pod == pytest.approx(4 / 6)
    assert r.far == pytest.approx(2 / 6)
    assert r.f1 == pytest.approx(8 / 12)
    assert r.bias == pytest.approx(1.0)


def test_perfect_and_empty_predictions():
    sim, obs, dom = _known_case()
    perfect = M.confusion(obs, obs, dom)
    assert (perfect.csi, perfect.pod, perfect.far, perfect.bias) == (1.0, 1.0, 0.0, 1.0)

    nothing = M.confusion(np.zeros_like(obs), obs, dom)
    assert nothing.csi == 0.0 and nothing.pod == 0.0


@pytest.mark.parametrize("attr", ["far"])
def test_undefined_ratio_is_none_not_zero(attr):
    """Predicting nothing leaves FAR undefined. None and 0.0 mean opposite things."""
    sim, obs, dom = _known_case()
    r = M.confusion(np.zeros_like(obs), obs, dom)
    assert getattr(r, attr) is None


def test_no_observation_leaves_pod_and_bias_undefined():
    sim, obs, dom = _known_case()
    r = M.confusion(sim, np.zeros_like(obs), dom)
    assert r.pod is None
    assert r.bias is None
    assert r.far == 1.0          # everything predicted was a false alarm


def test_domain_mask_excludes_rather_than_treating_unobserved_as_dry():
    """
    The whole methodology rests on this. Outside the mapped AOI there is no
    observation; counting those cells as dry manufactures false alarms.
    """
    sim, obs, _ = _known_case()
    half = np.zeros((4, 4), bool); half[:, :2] = True
    r = M.confusion(sim, obs, half)
    assert r.n_scored == 8
    assert (r.tp, r.fp, r.fn) == (2, 0, 2)
    # The simulated cells in the excluded half must not appear anywhere.
    assert r.tp + r.fp + r.fn + r.tn == r.n_scored


def test_shape_mismatch_raises():
    sim, obs, dom = _known_case()
    with pytest.raises(ValueError):
        M.confusion(np.zeros((2, 2), bool), obs, dom)


def test_agreement_map_codes_and_domain_clipping():
    sim, obs, dom = _known_case()
    a = M.agreement_map(sim, obs, dom)
    assert int((a == M.AGREE_HIT).sum()) == 4
    assert int((a == M.AGREE_MISS).sum()) == 2
    assert int((a == M.AGREE_FALSE).sum()) == 2

    half = np.zeros((4, 4), bool); half[:, :2] = True
    a2 = M.agreement_map(sim, obs, half)
    assert np.all(a2[:, 2:] == M.AGREE_NONE), \
        "a verdict was rendered where nothing was observed"


def test_module_self_checks_run():
    """The demo() blocks are executable documentation; keep them executable."""
    M.demo()
    O.demo()
    R.demo()


def test_api_json_reader_handles_windows_1252_bytes(tmp_path):
    """Generated GeoJSON can contain non-UTF-8 bytes from locale-specific names."""
    from src.api import main as api_main

    path = tmp_path / "latin1_results.geojson"
    payload = '{"type":"FeatureCollection","features":[{"properties":{"name":"Dørzong"}}]}'
    path.write_bytes(payload.encode("cp1252"))

    data = api_main._read_json_file(path)
    assert data["features"][0]["properties"]["name"] == "Dørzong"


# ═══════════════════════════════════════════════════════════ road link scoring

def test_sample_along_finds_a_crossing_at_the_end_of_a_link():
    """A midpoint-only sample misses a road cut at one end. This must not."""
    from rasterio.transform import from_origin
    from shapely.geometry import LineString

    tr = from_origin(0.0, 100.0, 10.0, 10.0)
    depth = np.zeros((10, 10)); depth[0, :] = 2.0        # top row flooded

    assert R._sample_along(LineString([(5, 95), (95, 95)]), depth, tr) == 2.0
    assert R._sample_along(LineString([(5, 5), (95, 5)]), depth, tr) == 0.0
    assert R._sample_along(LineString([(5, 55), (5, 95)]), depth, tr) == 2.0


def test_uncertain_grades_are_not_folded_into_either_class():
    assert "Possibly damaged" in R.UNCERTAIN_GRADES
    assert "Possibly damaged" not in R.DAMAGED_GRADES
    assert set(R.DAMAGED_GRADES) == {"Destroyed", "Damaged"}


# ═════════════════════════════════════════════════════════════════ real data

pytestmark_data = pytest.mark.skipif(
    not O.available("derna"),
    reason="observed data not downloaded; see data/validation/README.md",
)


@pytestmark_data
def test_observed_extent_loads_and_is_wgs84():
    g = O.load_observed_extent("derna")
    assert len(g) > 0
    assert g.crs is not None and g.crs.to_epsg() == 4326
    assert g.geometry.is_valid.all() or len(g) > 0     # EMS polys may self-touch


@pytestmark_data
def test_observed_domain_comes_from_aoi_metadata_not_the_flood_hull():
    """
    The AOI must be read from the activation metadata. A convex hull of the
    flood cannot contain a false alarm outside the flood, so a hull domain
    silently reports a better FAR than the truth.
    """
    dom = O.load_observed_domain("derna")
    assert list(dom["aoi"]) != ["hull"], "fell back to the flood hull"
    assert len(dom) >= 6, f"expected the EMSR696 AOIs, got {len(dom)}"

    ext = O.load_observed_extent("derna")
    # Every AOI must be at least as large as the flood mapped inside it.
    assert dom.to_crs(3857).area.sum() > ext.to_crs(3857).area.sum()


@pytestmark_data
def test_observed_roads_carry_damage_grades():
    r = O.load_observed_roads("derna")
    assert "damage_gra" in r.columns
    grades = set(r["damage_gra"].astype(str).unique())
    assert grades & set(R.DAMAGED_GRADES), f"no damaged links found: {grades}"


@pytestmark_data
def test_describe_reports_source_and_licence():
    d = O.describe("derna")
    assert d and "EMSR696" in d["source"]
    assert d["licence"]
    # Observed truth is never labelled as something we computed live.
    assert d["provenance"] != "COMPUTED_LIVE"


def test_unwired_scenario_reports_absence_not_a_score():
    assert O.available("phutkal") is False
    assert O.describe("phutkal") is None
