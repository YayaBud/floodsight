"""Cross-check each scenario's three sourced impoundment numbers against terrain.

Every scenario declares a crest elevation (geometry manifest, sourced), an
impounded volume (`SCENARIOS[key]['volume_mcm']`) and a lake length
(`SCENARIOS[key]['lake_formation']['length_km']`). Any two of them determine the
third once the DEM is fixed, so the three are a redundant set and can disagree.

`length_km` in particular is declared in `src/data_fetcher.py` and read by
nothing in the pipeline -- this script is the first consumer. It is free
evidence: a sourced number that no code depends on cannot have been tuned to
make anything pass.

Reports, per scenario, what the DEM gives at the sourced crest and what level
each of the other two numbers would require. Two numbers agreeing within a few
metres while the third needs tens or hundreds of metres identifies the outlier
without any judgement call.

Diagnostic only -- computes nothing the pipeline consumes, changes no state, and
deliberately does NOT fail a scenario. Whether a sourced-number disagreement
should become a validity gate is a design decision, not this script's to make.

Usage:  python scripts/diagnostics/check_sourced_consistency.py [coarsen]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import rasterio
import rasterio.features
from rasterio import Affine
from scipy import ndimage
from shapely.geometry import Point, shape
from shapely.ops import transform as shp_transform
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.data_fetcher import SCENARIOS                      # noqa: E402
from src.m2_geometry.dem_utils import condition_dem         # noqa: E402
from src.m2_geometry.validation import (                    # noqa: E402
    GeometryValidationError, validate_geometry)

# Two sourced numbers are treated as mutually consistent when the level each
# implies differs by less than this. It is a reporting threshold for the
# human-readable verdict line only -- every number is printed regardless, so
# the threshold cannot hide a disagreement.
_AGREE_TOL_M = 10.0


def _load(scenario_key: str, coarsen: int):
    dem_path = ROOT / "data" / "dem" / f"{scenario_key}_dem.tif"
    with rasterio.open(dem_path) as src:
        raw, transform, crs, nodata = src.read(1), src.transform, src.crs, src.nodata
    elev, _ = condition_dem(raw, nodata=nodata)
    if coarsen > 1:
        elev = elev[::coarsen, ::coarsen]
        transform = transform * Affine.scale(coarsen, coarsen)
    return elev, transform, crs


def _probe_factory(elev, transform, barrier_mask, seed_xy, river):
    """Volume and along-river extent of the upstream pool at a given level.

    The barrier is emplaced arbitrarily high rather than at its sourced crest so
    the basin's own hypsometry can be probed above the crest too -- otherwise
    every probe above the crest returns the merged downstream basin and the
    sweep is meaningless.
    """
    high = float(np.nanmax(elev)) + 10.0
    dem_work = np.where(barrier_mask, high, elev).astype(float)
    row, col = (int(v) for v in rasterio.transform.rowcol(transform, *seed_xy))
    pixel_area = abs(transform.a * transform.e)

    def probe(level: float) -> tuple[float, float, int]:
        below = dem_work < level
        if not below[row, col]:
            return 0.0, 0.0, 0
        labels, _ = ndimage.label(below)
        pool = labels == labels[row, col]
        volume = float(np.sum(np.where(pool, level - dem_work, 0.0)) * pixel_area)
        rows, cols = np.where(pool)
        xs, ys = rasterio.transform.xy(transform, rows, cols)
        along = [river.project(Point(x, y)) for x, y in zip(np.asarray(xs), np.asarray(ys))]
        length_km = (max(along) - min(along)) / 1000.0 if along else 0.0
        return volume / 1e6, length_km, int(pool.sum())

    return probe, float(dem_work[row, col]), high, pixel_area


def _level_for(probe, index: int, target: float, lo: float, hi: float) -> float:
    """Bisect for the level at which probe()[index] first reaches target."""
    for _ in range(90):
        mid = (lo + hi) / 2.0
        if probe(mid)[index] < target:
            lo = mid
        else:
            hi = mid
    return hi


def check(scenario_key: str, coarsen: int) -> None:
    cfg = SCENARIOS.get(scenario_key)
    if cfg is None:
        print(f"  SKIP {scenario_key}: no SCENARIOS entry")
        return
    lake = cfg.get("lake_formation") or {}
    sourced_volume = cfg.get("volume_mcm")
    sourced_length = lake.get("length_km")
    if sourced_volume is None or sourced_length is None:
        print(f"  SKIP {scenario_key}: needs both volume_mcm and "
              f"lake_formation.length_km (have {sourced_volume} / {sourced_length})")
        return

    manifest_path = ROOT / "data" / "geometry" / f"{scenario_key}.json"
    if not manifest_path.exists():
        print(f"  SKIP {scenario_key}: no geometry manifest")
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    elev, transform, crs = _load(scenario_key, coarsen)
    try:
        result = validate_geometry(manifest, elev, transform, crs)
    except GeometryValidationError as exc:
        print(f"  SKIP {scenario_key}: geometry gate refuses ({exc})")
        return

    to_dem = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    by_role: dict[str, list] = {}
    for feature in manifest["geometry"]["features"]:
        by_role.setdefault(feature["properties"]["role"], []).append(shape(feature["geometry"]))
    river = shp_transform(to_dem, by_role["river"][0])

    crest = float(result["crest_elev_m"])
    probe, bed, high, pixel_area = _probe_factory(
        elev, transform, result["barrier_mask"], result["upstream_seed_xy"], river)

    v_at_crest, l_at_crest, cells = probe(crest)
    level_for_volume = _level_for(probe, 0, float(sourced_volume), bed, high)
    level_for_length = _level_for(probe, 1, float(sourced_length), bed, high)
    v_at_len, l_at_len, _ = probe(level_for_length)
    v_at_vol, l_at_vol, _ = probe(level_for_volume)

    print(f"  cell {abs(transform.a):.1f} m | sourced crest {crest:.2f} m, "
          f"volume {sourced_volume} MCM, length {sourced_length} km, "
          f"dam_height_m {cfg.get('dam_height_m')}")
    print(f"    at sourced crest        {crest:9.2f} m  ->  V {v_at_crest:9.3f} MCM "
          f"({v_at_crest / sourced_volume * 100:6.1f}% sourced)  "
          f"L {l_at_crest:6.2f} km ({l_at_crest / sourced_length * 100:6.1f}% sourced)")
    print(f"    level for sourced VOLUME {level_for_volume:9.2f} m "
          f"({level_for_volume - crest:+8.2f} m vs crest)  -> L {l_at_vol:6.2f} km")
    print(f"    level for sourced LENGTH {level_for_length:9.2f} m "
          f"({level_for_length - crest:+8.2f} m vs crest)  -> V {v_at_len:9.3f} MCM")
    if cells:
        print(f"    implied mean depth at crest {v_at_crest * 1e6 / (cells * pixel_area):.1f} m")

    # Which pair agrees? The outlier is whichever number needs a level the other
    # two do not. Stated as an observation, never as a correction.
    d_vol = abs(level_for_volume - crest)
    d_len = abs(level_for_length - crest)
    if d_vol <= _AGREE_TOL_M and d_len <= _AGREE_TOL_M:
        print("    VERDICT: all three sourced numbers are mutually consistent")
    elif d_vol <= _AGREE_TOL_M:
        print(f"    VERDICT: crest and volume agree (within {d_vol:.2f} m); "
              f"length_km = {sourced_length} is the OUTLIER "
              f"(needs {d_len:.2f} m more head, implying {v_at_len:.1f} MCM)")
    elif d_len <= _AGREE_TOL_M:
        print(f"    VERDICT: crest and length agree (within {d_len:.2f} m); "
              f"volume_mcm = {sourced_volume} is the OUTLIER "
              f"(needs {d_vol:.2f} m more head, implying {l_at_vol:.2f} km)")
    else:
        print(f"    VERDICT: no two of the three agree "
              f"(volume needs {d_vol:.2f} m, length needs {d_len:.2f} m) "
              f"— the crest, or the basin itself, is in question")


def check_cascade_datum(scenario_key: str) -> None:
    """Compare a cascade reservoir's declared elevations against its own terrain.

    `SCENARIOS[key]['cascade']['reservoir']` declares z_bed_m / z_frl_m /
    z_crest_m independently of the geometry manifest's sourced `crest_elev_m`,
    and no code compares them -- so a cascade's release hydraulics and the
    terrain they are routed over can describe different worlds. Measured
    2026-09-19: rishiganga is 217.3 m apart (legitimately, they are different
    structures) and south_lhonak/Chungthang declares a reservoir crest
    **410.79 m below the lowest cell of the valley floor it stands in**, which
    is impossible rather than merely inconsistent.

    Run at native resolution: this reads the DEM over the barrier footprint and
    coarsening would only blur the floor it is measuring.
    """
    cascade = (SCENARIOS.get(scenario_key) or {}).get("cascade") or {}
    reservoir = cascade.get("reservoir")
    if not reservoir:
        return
    # Geometry is authored per structure for compound events, so a scenario may
    # own several manifests and none under its own key.
    manifests = sorted((ROOT / "data" / "geometry").glob(f"{scenario_key}*.json"))
    if not manifests:
        print(f"  cascade: declared but no geometry manifest to check it against")
        return
    dem_path = ROOT / "data" / "dem" / f"{scenario_key}_dem.tif"
    if not dem_path.exists():
        print(f"  cascade: no DEM at {dem_path.name}")
        return
    with rasterio.open(dem_path) as src:
        dem, transform, crs = src.read(1).astype(float), src.transform, src.crs
    to_dem = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform

    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        by_role: dict[str, list] = {}
        for feature in manifest["geometry"]["features"]:
            by_role.setdefault(feature["properties"]["role"], []).append(
                shape(feature["geometry"]))
        barrier_geoms = by_role.get("dam_body") or by_role.get("blockage")
        if not barrier_geoms:
            continue
        barrier = shp_transform(to_dem, barrier_geoms[0])
        mask = rasterio.features.rasterize(
            [(barrier, 1)], out_shape=dem.shape, transform=transform,
            fill=0, dtype="uint8").astype(bool)
        if not mask.any():
            continue
        floor = float(dem[mask].min())
        z_bed = reservoir.get("z_bed_m")
        z_crest = reservoir.get("z_crest_m")
        multi = len(manifests) > 1
        print(f"  cascade vs {manifest_path.stem}: barrier floor (min DEM over "
              f"footprint, {int(mask.sum())} cells) = {floor:.2f} m "
              f"[{reservoir.get('classification', '?')}]")
        if multi:
            print("      NOTE: this scenario has several structure manifests and one "
                  "cascade block, which can only describe one of them — a mismatch "
                  "here may mean the block belongs to a different structure")
        for label, value in (("z_bed_m", z_bed), ("z_crest_m", z_crest)):
            if value is None:
                continue
            delta = float(value) - floor
            if label == "z_bed_m" and delta < 0:
                # NOT an error. The DEM is a DSM: over an impoundment it returns
                # the WATER SURFACE, not the bed. A sourced bed below it is the
                # expected relationship and the gap is the water depth the DSM
                # is hiding. annamayya is the worked case -- z_bed_m 180.0 m
                # (OBSERVED, dam cross-section drawings) against a DEM floor of
                # 192.50 m, which memory.md:218 independently identifies as a
                # flat water plane. Flagging that as impossible would be crying
                # wolf on the one number here that is properly sourced.
                verdict = (f"expected for a DSM — implies {-delta:.2f} m of water "
                           "standing over the sourced bed")
            elif delta < 0:
                # A crest is the TOP of a structure. It cannot sit below the
                # lowest ground the structure stands on, whatever the DSM shows.
                verdict = "IMPOSSIBLE — a crest cannot be below its own foundation"
            else:
                verdict = "above floor"
            print(f"      {label} {float(value):9.2f} m  ->  {delta:+9.2f} m vs floor   {verdict}")


def main() -> None:
    coarsen = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    for scenario_key in sorted(SCENARIOS):
        print("=" * 78)
        print(f"SCENARIO {scenario_key}  (coarsen {coarsen})")
        try:
            check(scenario_key, coarsen)
        except Exception as exc:                              # noqa: BLE001
            print(f"  ERROR {type(exc).__name__}: {exc}")
        try:
            check_cascade_datum(scenario_key)
        except Exception as exc:                              # noqa: BLE001
            print(f"  cascade ERROR {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
