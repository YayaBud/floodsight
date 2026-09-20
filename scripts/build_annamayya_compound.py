"""Assemble the Annamayya COMPOUND run: lake formation + breach, one timeline.

The problem statement asks for lake formation and then the breach. The served
run (`annamayya_stage2_full`) had only the breach: 97 `stage: "routing"` frames
starting at T+0. This script prepends the sourced pre-breach reservoir rise from
`annamayya_stage1_frames.py` and writes the pair out as ONE run the UI can play
end to end.

Non-destructive by construction: `annamayya_stage2_full` is copied, never
modified, so the recorded baseline stays reproducible and this run can be thrown
away by deleting one directory. The copy is required, not laziness -- the API
resolves frames only inside the run's own directory
(`src/api/main.py::_registered_frame_path`), so a run cannot reference another
run's frames.

Run:  python scripts/build_annamayya_compound.py [--frames N] [--force]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio import Affine
from pyproj import Transformer
from shapely.geometry import LineString
from shapely.ops import transform as shp_transform

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import annamayya_stage1_frames as stage1          # noqa: E402
from src.m2_geometry.dem_utils import condition_dem   # noqa: E402
from src.run_manifest import (is_valid, load_manifest, register_artifact,   # noqa: E402
                              write_manifest)

RUNS = ROOT / "data" / "scenarios"
SOURCE_RUN = RUNS / "annamayya_stage2_full"
TARGET_RUN = RUNS / "annamayya_compound"
# Stage 1 renders at NATIVE resolution, not the routing run's 152.44 m.
#
# Frames do not have to share a grid -- each carries its own `preview_bounds`
# and its own GeoJSON, and the map places them independently. What Stage 1 DOES
# need is a grid on which the reservoir resolves, and 152 m is not one:
# decimating 5x merges the pool through the valley wall, and the FRL->crest
# storage comes out as 10,429 MCM against a true 15.96. At 30 m the same
# calculation gives 15.963 MCM and V(crest) = 59.735 MCM against a published
# gross storage of 63.43 MCM -- 94 %, the shortfall being the volume under the
# DSM's own water plane, which no DEM can see.
COARSEN = 1


def build_grid():
    """GLO-30 at native resolution, conditioned the same way the pipeline does."""
    with rasterio.open(ROOT / "data" / "dem" / "annamayya_dem.tif") as src:
        raw, transform, crs, nodata = src.read(1), src.transform, src.crs, src.nodata
    dem, _ = condition_dem(raw, nodata=nodata)
    if COARSEN > 1:
        dem = dem[::COARSEN, ::COARSEN]
        transform = transform * Affine.scale(COARSEN, COARSEN)
    return dem.astype(float), transform, crs


def dam_axis():
    """The sourced breach centerline, projected. 302 m, corroborated against
    the cited 336 m eroded embankment length (AP Irrigation Restoration DPR)."""
    ev = json.loads(stage1.EVIDENCE.read_text(encoding="utf-8"))
    line = ev["parameters"]["breach_centerline_wgs84"]
    with rasterio.open(ROOT / "data" / "dem" / "annamayya_dem.tif") as src:
        crs = src.crs
    to_dem = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    axis = shp_transform(to_dem, LineString(line))
    a, b = np.array(axis.coords[0]), np.array(axis.coords[-1])
    return tuple((a + b) / 2.0), (b - a)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=48,
                    help="pre-breach frames (default 48, vs 97 routing frames)")
    ap.add_argument("--force", action="store_true",
                    help="replace an existing annamayya_compound run")
    args = ap.parse_args()

    if not SOURCE_RUN.is_dir():
        print(f"source run missing: {SOURCE_RUN}")
        return 1
    if TARGET_RUN.exists():
        if not args.force:
            print(f"{TARGET_RUN.name} already exists — pass --force to replace")
            return 1
        shutil.rmtree(TARGET_RUN)
    print(f"copying {SOURCE_RUN.name} -> {TARGET_RUN.name}")
    shutil.copytree(SOURCE_RUN, TARGET_RUN)
    # `load_manifest` refuses a manifest whose run_id does not match its own
    # directory, so the copy has to be re-identified before it can be loaded.
    _raw = json.loads((TARGET_RUN / "manifest.json").read_text(encoding="utf-8"))
    _raw["run_id"] = TARGET_RUN.name
    (TARGET_RUN / "manifest.json").write_text(json.dumps(_raw, indent=2), encoding="utf-8")

    dem, transform, crs = build_grid()
    breach_xy, axis_dir = dam_axis()
    s = stage1.sourced()

    print(f"building {args.frames} pre-breach frames at dx={abs(transform.a):.1f} m")
    pre = stage1.write_frames(dem, transform, crs, breach_xy, axis_dir, s,
                              out_dir=TARGET_RUN, n_frames=args.frames)

    # Re-path the copied routing frames into this run and renumber after the
    # pre-breach block. The index stores absolute paths and the API refuses any
    # path outside the run directory, so this rewrite is mandatory.
    src_index = json.loads((SOURCE_RUN / "snapshots_index.json").read_text(encoding="utf-8"))
    post = []
    for j, fr in enumerate(src_index):
        fr = dict(fr)
        for key in ("path", "preview_path"):
            if fr.get(key):
                fr[key] = str(TARGET_RUN / Path(fr[key]).relative_to(SOURCE_RUN))
        fr["frame_idx"] = len(pre) + j
        post.append(fr)

    combined = pre + post
    (TARGET_RUN / "snapshots_index.json").write_text(
        json.dumps(combined, indent=1), encoding="utf-8")

    man = load_manifest(TARGET_RUN / "manifest.json")
    man["run_id"] = TARGET_RUN.name
    man["completed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pr = man.setdefault("pipeline_result", {})
    pr["snapshots_index"] = str(TARGET_RUN / "snapshots_index.json")
    pr["stage1_prebreach"] = str(TARGET_RUN / "stage1_prebreach.json")

    # The pre-breach half is prescribed, and the manifest must say so next to
    # the forced-hydrograph note that already covers the post-breach half.
    diag = json.loads((TARGET_RUN / "stage1_prebreach.json").read_text(encoding="utf-8"))["diagnostics"]
    reasons = list((man.get("validity") or {}).get("reasons") or [])
    reasons.append(
        "PRE-BREACH STAGE IS PRESCRIBED, not solved. Level is pinned to the "
        "sourced event clock (FRL 203.6 m at T-180 min, crest 206.0 m at "
        "T-45 min, both OBSERVED) with the shape taken from a mass balance. "
        + str(diag.get("mass_balance_note", "")))
    man.setdefault("validity", {})["reasons"] = reasons
    man["validity"]["prebreach_stage_prescribed"] = True

    for name, path, media in (
            ("snapshots_index", TARGET_RUN / "snapshots_index.json", "application/json"),
            ("stage1_prebreach", TARGET_RUN / "stage1_prebreach.json", "application/json")):
        register_artifact(man, path, run_root=RUNS, name=name, media_type=media)

    write_manifest(man, RUNS)
    ok = is_valid(load_manifest(TARGET_RUN / "manifest.json"), run_root=RUNS)

    print(f"\n{len(pre)} pre-breach + {len(post)} routing = {len(combined)} frames")
    print(f"  t_min {combined[0]['t_min']:+.1f} -> {combined[-1]['t_min']:+.1f}")
    print(f"  stages: {sorted({f['stage'] for f in combined})}")
    print(f"  storage FRL->crest  {diag['storage_frl_to_crest_m3']/1e6:8.3f} MCM")
    print(f"  sourced net inflow  {diag['net_volume_m3']/1e6:8.3f} MCM "
          f"({100*diag['sourced_fraction_of_required']:.0f}% of required)")
    print(f"  manifest is_valid: {ok}")
    if not ok:
        print("  -> the API will NOT serve this run")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
