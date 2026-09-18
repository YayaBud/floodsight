"""How much of the reconstructed flood corridor did a run actually wet?

    python scripts/corridor_coverage.py data/scenarios/annamayya_stage2_wide

READ THIS BEFORE QUOTING A NUMBER FROM IT
-----------------------------------------
`data/admin/annamayya_corridor.geojson` is **terrain-derived**: three HAND stage
bands, computed from the DEM. It is NOT an observation. No satellite saw the
Annamayya flood -- Sentinel-1 path 92 acquired 16 Nov and 28 Nov and the event
sits in the 12-day gap; Sentinel-2 passed 4 h after the breach into 98.7 % cloud
(INVARIANTS.md S3). The corridor is shipped to the UI as its own layer kind for
exactly this reason and must never be served as `observed`.

So coverage against it is a **diagnostic**, not a skill score:

  * It answers "did the flood reach the ground the terrain says a flood of this
    size would occupy" -- useful for finding a truncated domain or a stalled front.
  * It does NOT answer "is the model right". Raising it by tuning would be
    fitting a model to a reconstruction produced from the same DEM the model
    runs on, which is circular.

Never compute a CSI against it and never call the result validation.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import rasterio
import rasterio.features

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WET_M = 0.30          # the depth the rest of the project calls "flooded"


def _load_depth(run_dir: Path, name: str = "max_depth.tif"):
    with rasterio.open(run_dir / name) as src:
        return src.read(1).astype(float), src.transform, src.crs


def _rasterize(gdf_geoms, shape, transform, crs, value=1):
    return rasterio.features.rasterize(
        ((g, value) for g in gdf_geoms), out_shape=shape, transform=transform,
        fill=0, dtype="uint8").astype(bool)


def corridor_bands(crs):
    import geopandas as gpd
    g = gpd.read_file(ROOT / "data" / "admin" / "annamayya_corridor.geojson").to_crs(crs)
    return g.sort_values("code")


def front_along_stem(wet, transform, crs):
    """Furthest point down the mapped Cheyyeru stem below the dam that is wet."""
    from shapely.geometry import Point
    from pyproj import Transformer
    try:
        from src.data_fetcher import build_rivers
        riv = build_rivers("annamayya")
        stale = " [RIVER LAYER STALE FOR THIS BBOX]" if riv.attrs.get("bbox_stale") else ""
        riv = riv.to_crs(crs)
    except Exception as exc:                               # noqa: BLE001
        return None, f"rivers unavailable ({str(exc)[:60]})"
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    dam = Point(fwd.transform(79.02128, 14.21059))
    reaches = [g for n, g in zip(riv.get("name"), riv.geometry) if str(n) == "Cheyyeru"]
    if not reaches:
        return None, "no reach named Cheyyeru in the river layer"
    # the downstream stem is the reach that STARTS at the dam
    stem = min(reaches, key=lambda g: Point(g.coords[0]).distance(dam))
    if Point(stem.coords[-1]).distance(dam) < Point(stem.coords[0]).distance(dam):
        from shapely.geometry import LineString
        stem = LineString(list(stem.coords)[::-1])
    rr, cc = np.nonzero(wet)
    if not len(rr):
        return 0.0, f"stem {stem.length/1000:.1f} km, nothing wet{stale}"
    xs, ys = rasterio.transform.xy(transform, rr, cc)
    best = 0.0
    for x, y in zip(np.asarray(xs), np.asarray(ys)):
        p = Point(x, y)
        if p.distance(stem) <= max(abs(transform.a), abs(transform.e)) * 1.5:
            best = max(best, stem.project(p))
    return best / 1000.0, f"stem {stem.length/1000:.1f} km{stale}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir")
    ap.add_argument("--wet-m", type=float, default=WET_M)
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--no-front", action="store_true",
                    help="skip the stem front, which needs the OSM river layer and "
                         "will block on a refetch when Overpass is throttling")
    a = ap.parse_args()

    run = ROOT / a.run_dir if not Path(a.run_dir).is_absolute() else Path(a.run_dir)
    depth, tr, crs = _load_depth(run)
    dx, dy = abs(tr.a), abs(tr.e)
    cell_km2 = dx * dy / 1e6
    wet = depth > a.wet_m

    print("=" * 72)
    print(f"CORRIDOR COVERAGE -- {run.name}")
    print("=" * 72)
    print(f"grid {depth.shape} at {dx:.1f} m; wet threshold {a.wet_m:.2f} m")
    print(f"modelled wet area              {wet.sum()*cell_km2:9.2f} km2")
    print(f"max depth                      {np.nanmax(depth):9.2f} m")
    print()
    print("The corridor is HAND-derived terrain, NOT an observation. Coverage is a")
    print("diagnostic for reach and truncation -- never a skill score.")
    print()

    bands = corridor_bands(crs)
    total_mask = np.zeros(depth.shape, bool)
    rows = []
    for _, b in bands.iterrows():
        m = _rasterize([b.geometry], depth.shape, tr, crs)
        total_mask |= m
        area = m.sum() * cell_km2
        hit = (m & wet).sum() * cell_km2
        rows.append((b["class"], area, hit, 100 * hit / area if area else 0.0))
    t_area = total_mask.sum() * cell_km2
    t_hit = (total_mask & wet).sum() * cell_km2
    print(f"{'band':16} {'in-domain km2':>14} {'wetted km2':>12} {'cover %':>9}")
    for cls, area, hit, pct in rows:
        print(f"{cls:16} {area:14.2f} {hit:12.2f} {pct:9.1f}")
    print(f"{'UNION':16} {t_area:14.2f} {t_hit:12.2f} "
          f"{100*t_hit/t_area if t_area else 0:9.1f}")
    print()
    print(f"wet OUTSIDE the corridor       {((~total_mask) & wet).sum()*cell_km2:9.2f} km2"
          f"   ({100*((~total_mask)&wet).sum()/max(1,wet.sum()):.1f} % of the wet area)")

    km, note = (None, "skipped (--no-front)") if a.no_front else front_along_stem(wet, tr, crs)
    print()
    if km is None:
        print(f"front on the downstream stem   NOT COMPUTED -- {note}")
    else:
        print(f"front on the downstream stem   {km:9.2f} km   ({note})")

    # settlements
    import geopandas as gpd
    vil = gpd.read_file(ROOT / "data" / "admin" / "annamayya_villages.geojson").to_crs(crs)
    hit = 0
    depths = []
    for _, r in vil.iterrows():
        c = r.geometry.centroid
        col = int((c.x - tr.c) / tr.a)
        row = int((c.y - tr.f) / tr.e)
        d = float(depth[row, col]) if (0 <= row < depth.shape[0] and 0 <= col < depth.shape[1]) else 0.0
        depths.append((r.get("village_name", "?"), d))
        hit += d > a.wet_m
    print(f"settlements > {a.wet_m:.2f} m           {hit:9d} / {len(vil)}")
    dry = [n for n, d in depths if d <= a.wet_m]
    print(f"  dry: {', '.join(str(n).encode('ascii','ignore').decode().strip()[:18] for n in dry)}")

    if a.json_out:
        Path(a.json_out).write_text(json.dumps({
            "run": run.name, "wet_threshold_m": a.wet_m,
            "wet_km2": float(wet.sum() * cell_km2),
            "max_depth_m": float(np.nanmax(depth)),
            "corridor_union_km2": float(t_area),
            "corridor_wetted_km2": float(t_hit),
            "corridor_cover_pct": float(100 * t_hit / t_area) if t_area else None,
            "bands": [{"class": c, "area_km2": a_, "wet_km2": h, "cover_pct": p}
                      for c, a_, h, p in rows],
            "front_km_on_stem": km,
            "settlements_wet": int(hit), "settlements_total": int(len(vil)),
            "corridor_is_terrain_derived_not_observed": True,
        }, indent=1), encoding="utf-8")
        print(f"\nwrote {a.json_out}")


if __name__ == "__main__":
    main()
