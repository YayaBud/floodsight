"""Densify a run's post-breach frames so the flood front advances continuously.

The problem, measured on `annamayya_compound`: post-breach frames are **15
minutes apart** (`save_interval_s = 900`). The front travels ~2.5 km -- about
16 cells at dx = 152 m -- between consecutive frames, so playback reads as a
series of jumps, and three settlements (T+104, T+109, T+118) flip to "hit" on a
single frame swap. No amount of cross-fading fixes a 15-minute step; the fix is
temporal resolution.

Why not just linearly blend the two depth fields
------------------------------------------------
Pixel-wise interpolation between two depth grids makes the newly-wet area FADE
IN at partial depth everywhere at once -- a dissolve, not motion, and it puts
shallow water on ground the wave has not reached yet. That would be a visual
claim the solver never made.

So the front is driven by an ARRIVAL-TIME field instead, computed from the run's
own depth stack: for each cell, the time its depth first crosses the wet
threshold, linearly interpolated between the two bracketing frames for
sub-frame precision. Then at any time t:

    wet(t)   = arrival <= t              <- the front's POSITION, from the solver
    depth(t) = lerp(depth[k], depth[k+1]) masked by wet(t)

The front therefore advances cell by cell at the time the solver says it
arrived, and only the depth MAGNITUDE inside the already-wet area is
interpolated. Both halves stay inside what the run computed.

Interpolated frames are labelled `INTERPOLATED_FOR_DISPLAY`; frames that land
exactly on a solver output keep their original provenance. Nothing here is
presented as new solver output.

Run:  python scripts/densify_frames.py [run_id] [--step 3] [--until 240]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.run_manifest import (is_valid, load_manifest,          # noqa: E402
                              register_artifact, write_manifest)

RUNS = ROOT / "data" / "scenarios"
WET_M = 0.30          # the display threshold the rest of the pipeline uses


def load_depth_stack(run: Path, post_frames: list[dict]):
    """The run's own depth rasters, in frame order, with their times."""
    paths = sorted((run / "depth_rasters").glob("depth_*.tif"))
    if len(paths) != len(post_frames):
        print(f"  note: {len(paths)} depth rasters vs {len(post_frames)} post frames "
              f"-- pairing the first {min(len(paths), len(post_frames))}")
    n = min(len(paths), len(post_frames))
    with rasterio.open(paths[0]) as src:
        transform, crs, nodata = src.transform, src.crs, src.nodata
    stack = np.empty((n,) + rasterio.open(paths[0]).shape, dtype="float32")
    for i in range(n):
        with rasterio.open(paths[i]) as src:
            d = src.read(1).astype("float32")
        if nodata is not None:
            d = np.where(d == nodata, 0.0, d)
        stack[i] = np.nan_to_num(d, nan=0.0)
    times = np.array([float(post_frames[i]["t_min"]) for i in range(n)])
    return stack, times, transform, crs


def arrival_field(stack: np.ndarray, times: np.ndarray) -> np.ndarray:
    """Minute each cell first exceeds WET_M, interpolated between frames.

    np.inf where the cell never wets, so `arrival <= t` is simply false there
    for every t and the front never leaks into dry ground.
    """
    n = stack.shape[0]
    arrival = np.full(stack.shape[1:], np.inf, dtype="float32")
    wet_prev = stack[0] > WET_M
    arrival[wet_prev] = times[0]
    for k in range(1, n):
        wet = stack[k] > WET_M
        newly = wet & ~wet_prev & np.isinf(arrival)
        if newly.any():
            d0, d1 = stack[k - 1][newly], stack[k][newly]
            # Fraction of the interval at which depth crosses the threshold.
            denom = np.where((d1 - d0) == 0, 1e-9, d1 - d0)
            frac = np.clip((WET_M - d0) / denom, 0.0, 1.0)
            arrival[newly] = times[k - 1] + frac * (times[k] - times[k - 1])
        wet_prev = wet_prev | wet
    return arrival


def depth_at(stack, times, arrival, t: float) -> np.ndarray:
    """Interpolated depth at time t, clipped to the front's actual position."""
    k = int(np.clip(np.searchsorted(times, t) - 1, 0, len(times) - 2))
    span = times[k + 1] - times[k]
    f = 0.0 if span <= 0 else float(np.clip((t - times[k]) / span, 0.0, 1.0))
    d = (1.0 - f) * stack[k] + f * stack[k + 1]
    return np.where(arrival <= t, d, 0.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id", nargs="?", default="annamayya_compound")
    ap.add_argument("--step", type=float, default=3.0,
                    help="minutes between densified frames (default 3)")
    ap.add_argument("--until", type=float, default=240.0,
                    help="densify from T+0 to this minute; beyond it the "
                         "original spacing is kept (default 240)")
    args = ap.parse_args()

    run = RUNS / args.run_id
    idx_path = run / "snapshots_index.json"
    if not idx_path.is_file():
        print(f"no snapshots_index at {run}")
        return 1
    frames = json.loads(idx_path.read_text(encoding="utf-8"))
    pre = [f for f in frames if f.get("stage") == "reservoir_rise"]
    post = [f for f in frames if f.get("stage") != "reservoir_rise"]
    if not post:
        print("no post-breach frames")
        return 1
    print(f"{args.run_id}: {len(pre)} pre-breach + {len(post)} post-breach")

    stack, times, transform, crs = load_depth_stack(run, post)
    arrival = arrival_field(stack, times)
    finite = np.isfinite(arrival)
    print(f"  arrival field: {int(finite.sum()):,} cells ever wet, "
          f"{arrival[finite].min():.1f} -> {arrival[finite].max():.1f} min")

    import run_pipeline as _rp
    to_wgs84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
    snaps, prev = run / "snapshots", run / "preview_rasters"
    snaps.mkdir(exist_ok=True); prev.mkdir(exist_ok=True)

    # Target times: dense to --until, then the original frame times after it.
    dense = list(np.arange(float(times[0]), min(args.until, float(times[-1])) + 1e-9,
                           args.step))
    tail = [float(t) for t in times if t > args.until]
    targets = sorted(set([round(t, 3) for t in dense + tail]))
    exact = {round(float(t), 3) for t in times}
    print(f"  {len(post)} -> {len(targets)} post-breach frames "
          f"({args.step:g} min to T+{args.until:g}, original spacing after)")

    new_post = []
    for j, t in enumerate(targets):
        d = depth_at(stack, times, arrival, t)
        fp = snaps / f"frame_d{j:04d}.geojson"
        pp = prev / f"frame_d{j:04d}.png"
        fp.write_text(json.dumps(_rp._depth_to_geojson(d, transform, t * 60.0, to_wgs84)),
                      encoding="utf-8")
        bounds = _rp._write_depth_preview(d, pp, transform, to_wgs84)
        is_exact = round(t, 3) in exact
        new_post.append({
            "frame_idx": len(pre) + j,
            "t_s": float(t * 60.0), "t_min": round(float(t), 1),
            "stage": "routing",
            "phase_title": ("Release begins (T+0 min)" if t <= 0
                            else f"Flood routing (T+{t:.0f} min)"),
            "path": str(fp), "preview_path": str(pp), "preview_bounds": bounds,
            "provenance": ("FORCED_HYDROGRAPH_INUNDATION" if is_exact
                           else "INTERPOLATED_FOR_DISPLAY"),
        })

    for i, f in enumerate(pre):
        f["frame_idx"] = i
    combined = pre + new_post
    idx_path.write_text(json.dumps(combined, indent=1), encoding="utf-8")

    # The arrival field is real solver output re-expressed; keep it.
    arr_path = run / "arrival_time.tif"
    prof = {"driver": "GTiff", "height": arrival.shape[0], "width": arrival.shape[1],
            "count": 1, "dtype": "float32", "crs": crs, "transform": transform,
            "nodata": -9999.0, "compress": "lzw"}
    with rasterio.open(arr_path, "w", **prof) as dst:
        dst.write(np.where(np.isfinite(arrival), arrival, -9999.0).astype("float32"), 1)

    man = load_manifest(run / "manifest.json")
    man.setdefault("pipeline_result", {})["arrival_time_tif"] = str(arr_path)
    for name, path, media in (("snapshots_index", idx_path, "application/json"),
                              ("arrival_time", arr_path, "image/tiff")):
        register_artifact(man, path, run_root=RUNS, name=name, media_type=media)
    write_manifest(man, RUNS)

    ok = is_valid(load_manifest(run / "manifest.json"), run_root=RUNS)
    interp = sum(1 for f in new_post if f["provenance"] == "INTERPOLATED_FOR_DISPLAY")
    print(f"\n{len(combined)} frames total "
          f"({len(pre)} pre + {len(new_post)} post, {interp} interpolated)")
    print(f"  t_min {combined[0]['t_min']:+.1f} -> {combined[-1]['t_min']:+.1f}")
    print(f"  manifest is_valid: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
