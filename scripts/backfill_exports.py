"""Back-fill .shp / .kml / CAP exports onto an existing run, and register them.

PS 26161 deliverable (iii) names .shp and .kml explicitly. The route
`/api/export/{job_id}/{fmt}` has always been correct, but it reads
`pipeline_result.shp / .kml / .cap / .max_depth_tif`, and the annamayya run had
none of them -- `route_annamayya.py::write_exports` was never run for it, so the
one Indian run we actually demonstrate had nothing to download.

`write_exports` was already written to be back-filled ("without re-solving 24 h
of shallow water", its own docstring). This script is the caller it never had.

Run:  python scripts/backfill_exports.py [run_id]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from src.run_manifest import (is_valid, load_manifest,          # noqa: E402
                              register_artifact, write_manifest)

RUNS = ROOT / "data" / "scenarios"


def main() -> int:
    run_id = sys.argv[1] if len(sys.argv) > 1 else "annamayya_compound"
    out = RUNS / run_id
    if not (out / "manifest.json").is_file():
        print(f"no manifest at {out}")
        return 1

    # Imported here, not at module scope: route_annamayya pulls in the whole
    # solver stack and this script is also useful for inspecting a run.
    from route_annamayya import write_exports
    import geopandas as gpd

    with rasterio.open(out / "max_depth.tif") as src:
        max_depth = src.read(1).astype(float)
        transform, crs, nodata = src.transform, src.crs, src.nodata
    if nodata is not None:
        max_depth = np.where(max_depth == nodata, 0.0, max_depth)
    to_wgs84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform

    vil = gpd.read_file(ROOT / "data" / "admin" / "annamayya_villages.geojson").to_crs(crs)

    # Released volume and peak come from the run's OWN hydrograph, not from a
    # re-derivation -- the export's provenance block quotes them, so they have
    # to be the numbers this run actually carried.
    hyd = json.loads((out / "hydrograph.json").read_text(encoding="utf-8"))
    arm = hyd.get("measured") or hyd.get("central")
    t_s = np.asarray(arm["t_s"], dtype=float)
    q = np.asarray(arm.get("Q_m3s") or arm.get("q_m3s") or arm["Q"], dtype=float)
    released = float(np.trapezoid(q, t_s))
    peak = float(q.max())
    print(f"{run_id}: released {released/1e6:.2f} MCM, peak {peak:.1f} m3/s, "
          f"max depth {max_depth.max():.2f} m")

    shp, kml, extent, cap = write_exports(
        out, vil, max_depth, transform, to_wgs84,
        released_volume_m3=released, peak_q_m3s=peak, t_s=float(t_s[-1]))

    man = load_manifest(out / "manifest.json")
    pr = man.setdefault("pipeline_result", {})
    # Serve the ZIP, not the bare .shp. A shapefile is five files; handing a
    # judge a lone .shp with no .dbf/.shx/.prj gives them something no GIS can
    # open, and `export_shp` already bundles the set plus its provenance JSON.
    shp_zip = Path(shp).with_suffix(".zip")
    pr["shp"] = str(shp_zip if shp_zip.exists() else shp)
    pr["kml"] = str(kml)
    pr["cap"] = str(cap)
    pr["max_depth_tif"] = str(out / "max_depth.tif")
    if extent:
        ext_zip = Path(extent["shp"]).with_suffix(".zip")
        pr["inundation_shp"] = str(ext_zip if ext_zip.exists() else extent["shp"])
        pr["inundation_kml"] = extent["kml"]

    for name, path, media in (
            ("exports_shp", Path(pr["shp"]), "application/zip"),
            ("exports_kml", Path(kml), "application/vnd.google-earth.kml+xml"),
            ("exports_cap", Path(cap), "application/json")):
        if path.exists():
            register_artifact(man, path, run_root=RUNS, name=name, media_type=media)
    write_manifest(man, RUNS)

    ok = is_valid(load_manifest(out / "manifest.json"), run_root=RUNS)
    print(f"registered shp/kml/cap/tif; manifest is_valid: {ok}")
    for k in ("shp", "kml", "cap", "max_depth_tif"):
        p = Path(pr[k])
        print(f"  {k:14s} {p.name:58s} {p.stat().st_size if p.exists() else 'MISSING':>10}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
