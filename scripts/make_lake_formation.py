"""Build the pre-breach lake-formation animation for one scenario.

Standalone on purpose: a pre-breach pool needs a DEM, a seed, a crest and a
stage range. It does NOT need the 2D solver, so it must not need a run that
clears every solver gate — today no scenario does, and the lake frames were
therefore unreachable for every scenario but phutkal.

Two extent backends, because the two scenarios are physically different and the
difference is the whole provenance story:

``dem_fill``  (phutkal)
    The production path. ``validate_geometry`` proves the barrier separates the
    seeds, the barrier is raised to its sourced crest, and the pool is a seeded
    flood fill of real terrain. The shoreline IS the DEM's.

``hypsometric``  (annamayya)
    The DEM cannot do this. GLO-30 here is a DSM captured with the reservoir
    full: it holds the water surface, not the dam and not the bed beneath the
    pool. Terrain at or below the 206 m crest covers 740 km2 in 28 components,
    the valley at the dam's latitude is 25.1 km wide, and the dam footprint is
    366 m -- so ``validate_geometry`` refuses, and a naive fill to 203.60 m
    returns 1,720 km2 / 604,982 MCM against a configured 63.43 MCM.

    So the extent is not taken from terrain alone. It is a priority flood that
    grows from the reservoir surface the DSM actually observed, in ascending
    elevation order, capped by the area the OFFICIAL stage-storage curve says
    the reservoir has at that level:

        V(z) = V_frl * ((z - z_bed) / (z_frl - z_bed)) ** alpha        [OBSERVED]
        A(z) = dV/dz

    Terrain decides the SHAPE, the sourced curve decides HOW MUCH. The two
    agree where they can be compared: A(192.50) = 2.590 km2 against the 2.315
    km2 of DSM water plane actually in the raster, 89.4 %. That agreement is
    what makes the cap an anchor rather than a fudge factor.

    The reconstructed shoreline is NOT observed and is labelled
    RECONSTRUCTED_HYPSOMETRIC everywhere it is written. It must never be served
    through the `observed` layer kind and must never be scored a CSI, for the
    same reasons already recorded for the corridor and observed_wse layers.

Frames are spaced by equal VOLUME, never by equal stage. Hypsometry is steep:
on phutkal the bottom 29 % of the height range holds 12.9 % of the water and
the top 29 % holds 47 %, so equal-stage frames spend half the animation on an
invisible puddle. That is exactly what the old four-frame output did --
0.00, 0.00, 0.25, 19.54 MCM.

Usage:
    python scripts/make_lake_formation.py phutkal
    python scripts/make_lake_formation.py annamayya --frames 24
    python scripts/make_lake_formation.py phutkal --check
"""
from __future__ import annotations

import argparse
import heapq
import json
import logging
import sys
import warnings
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from scipy import ndimage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data"

from run_pipeline import _depth_to_geojson, _write_depth_preview   # noqa: E402
from src.data_fetcher import SCENARIOS                             # noqa: E402
from src.m2_geometry.dem_utils import condition_dem                # noqa: E402
from src.m2_geometry.fill import compute_lake_depth_grids          # noqa: E402
from src.m2_geometry.fill import equal_volume_levels          # noqa: E402
from src.m2_geometry.validation import validate_geometry           # noqa: E402

logger = logging.getLogger("lake_formation")

# The DSM water plane that stands in for Annamayya's reservoir surface. It is a
# real observation -- the elevation Copernicus recorded over the pool -- and it
# is also the reason the dam is absent. Matched to 5 mm because the plane is a
# single repeated float, not a range.
_PLANE_ATOL_M = 0.005


# ── level selection ───────────────────────────────────────────────────────────

# `equal_volume_levels` used to live here. It now lives in
# `src/m2_geometry/fill.py` because `run_pipeline.py` needs the same physics:
# while it was a script-local function the pipeline kept its own STAGE-fraction
# version, and the API served four frames (two of them empty) while this script
# produced 24 usable ones off the same DEM. One implementation, one behaviour.


def _observed_plane(z: np.ndarray, plane_m: float, near_rc: tuple[int, int]) -> np.ndarray:
    """The connected DSM water-plane component that represents the reservoir."""
    flat = np.isclose(z, plane_m, atol=_PLANE_ATOL_M)
    lab, n = ndimage.label(flat)
    if n == 0:
        raise SystemExit(f"no cells at the {plane_m} m water plane — wrong DEM?")
    comp = int(lab[near_rc])
    if comp == 0:                       # seed cell is off the plane; take the largest
        sizes = ndimage.sum(flat, lab, range(1, n + 1))
        comp = int(np.argmax(sizes)) + 1
    return lab == comp


def _priority_flood(z: np.ndarray, seed: np.ndarray, level: float,
                    max_cells: int, floor_m: float | None = None) -> np.ndarray:
    """Grow from `seed` in ascending elevation, stopping at `level` or `max_cells`.

    `floor_m` excludes terrain BELOW the captured water plane, and it is
    load-bearing rather than cosmetic. Growing lowest-first from the plane
    otherwise walks straight down the Cheyyeru channel — cells at 178-192 m,
    downstream of where the dam should be and outside the impoundment entirely —
    and each is counted at 12-27 m deep. Unconstrained, the reconstruction
    measured ~169 MCM against a sourced 63.43.

    The DSM is what justifies the cut: it captured the reservoir FULL, so any
    cell lying below that water surface cannot be part of the impoundment, or it
    would have been captured as water too. The pool therefore grows only into
    terrain at or above the plane.

    One algorithm covers both binding regimes, which is why the cap is safe:

    * terrain binds  -> the frontier runs out of cells at or below `level`
      before the area cap is reached, and the pool is simply what the basin
      holds. This is what happens below ~203 m.
    * sourced curve binds -> the cap is hit first, and the pool stops growing
      instead of spilling onto the 25 km plain. This is what happens above it.

    Popping the lowest frontier cell keeps the result 4-connected to the seed,
    so the pool can never be a scatter of disconnected low spots.
    """
    ny, nx = z.shape
    pool = seed.copy()
    count = int(pool.sum())
    heap: list[tuple[float, int, int]] = []
    pushed = pool.copy()

    def push_neighbours(r: int, c: int) -> None:
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            rr, cc = r + dr, c + dc
            if 0 <= rr < ny and 0 <= cc < nx and not pushed[rr, cc]:
                pushed[rr, cc] = True
                if floor_m is not None and z[rr, cc] < floor_m:
                    continue            # below the captured plane: not impoundment
                heapq.heappush(heap, (float(z[rr, cc]), rr, cc))

    for r, c in zip(*np.nonzero(seed)):
        push_neighbours(int(r), int(c))

    while heap and count < max_cells:
        elev, r, c = heapq.heappop(heap)
        if elev > level:                # terrain binds: nothing left under water
            break
        pool[r, c] = True
        count += 1
        push_neighbours(r, c)
    return pool


def hypsometric_frames(key: str, levels: list[float], z: np.ndarray, transform,
                       plane_m: float, near_rc: tuple[int, int],
                       margin_cells: int = 40) -> tuple[list[dict], object]:
    res = SCENARIOS[key]["cascade"]["reservoir"]
    z_bed, z_frl = float(res["z_bed_m"]), float(res["z_frl_m"])
    v_frl, alpha = float(res["v_frl_mcm"]) * 1e6, float(res["alpha_exp"])
    px_area = abs(transform.a * transform.e)

    def area_of(lv: float) -> float:            # A(z) = dV/dz
        s = max((lv - z_bed) / (z_frl - z_bed), 0.0)
        return v_frl * alpha / (z_frl - z_bed) * s ** (alpha - 1.0)

    seed = _observed_plane(z, plane_m, near_rc)
    logger.info("observed DSM water plane at %.2f m: %d cells = %.3f km2 "
                "(analytic A = %.3f km2, %.1f %% agreement)",
                plane_m, int(seed.sum()), seed.sum() * px_area / 1e6,
                area_of(plane_m) / 1e6, 100 * seed.sum() * px_area / area_of(plane_m))

    # Crop to the reservoir before rendering. The pool tops out around 8,400
    # cells in a 2372x1647 domain, so a full-domain frame is 99.8 % empty and
    # the preview writer upsamples all of it 3x -- that, not the flood, is what
    # makes this slow. Cropping also means the preview actually frames the
    # reservoir instead of a speck. `preview_bounds` is georeferenced off the
    # cropped transform, so the map still places it correctly.
    top = _priority_flood(z, seed, levels[-1],
                          int(round(area_of(levels[-1]) / px_area)), floor_m=plane_m)
    rows, cols = np.nonzero(top)
    r0 = max(0, int(rows.min()) - margin_cells); r1 = min(z.shape[0], int(rows.max()) + margin_cells + 1)
    c0 = max(0, int(cols.min()) - margin_cells); c1 = min(z.shape[1], int(cols.max()) + margin_cells + 1)
    z = z[r0:r1, c0:c1]
    seed = seed[r0:r1, c0:c1]
    transform = transform * rasterio.Affine.translation(c0, r0)
    logger.info("cropped to the reservoir: %dx%d cells (was %dx%d)",
                r1 - r0, c1 - c0, *top.shape)

    out = []
    for lv in levels:
        cap = int(round(area_of(lv) / px_area))
        pool = _priority_flood(z, seed, lv, cap, floor_m=plane_m)
        depth = np.where(pool, np.maximum(0.0, lv - z), 0.0).astype(np.float32)
        n = int(pool.sum())
        out.append({
            "level_m": float(lv),
            "area_m2": n * px_area,
            "volume_m3": float(depth.sum()) * px_area,
            "depth_grid": depth,
            # Which side actually limited this frame. Worth carrying: it is the
            # difference between "the basin holds this" and "the sourced curve
            # says stop", and a reader should not have to guess which.
            "binding": "SOURCED_AREA_CURVE" if n >= cap else "TERRAIN",
            "area_cap_m2": cap * px_area,
        })
    return out, transform


# ── backend: DEM fill through the production geometry gate ───────────────────

def dem_fill_frames(key: str, n_frames: int, coarsen: int) -> tuple[list[dict], dict]:
    with rasterio.open(DATA / "dem" / f"{key}_dem.tif") as src:
        raw, transform, crs, nodata = src.read(1), src.transform, src.crs, src.nodata
    dem, _wall = condition_dem(raw, nodata=nodata)
    if coarsen > 1:
        dem = dem[::coarsen, ::coarsen]
        transform = transform * rasterio.Affine.scale(coarsen, coarsen)

    manifest = json.loads((DATA / "geometry" / f"{key}.json").read_text(encoding="utf-8"))
    gr = validate_geometry(manifest, dem, transform, crs)
    crest = float(gr["crest_elev_m"])
    barrier = np.asarray(gr["barrier_mask"], dtype=bool)
    seed_xy = gr["upstream_seed_xy"]
    wse = float(SCENARIOS[key]["wse_m"])
    logger.info("geometry gate PASSED — crest %.2f m [%s], %d barrier cells",
                crest, gr["crest_elev_classification"], int(barrier.sum()))

    # The fill's own floor, measured the way compute_lake_depth_grids measures
    # it, so the probe range matches the thing being probed.
    work = np.where(barrier & (dem < crest), crest, dem)
    row, col = rasterio.transform.rowcol(transform, *seed_xy)
    lab, _ = ndimage.label(work < wse)
    full = lab == lab[row, col]
    z_min = float(work[full].min())
    px_area = abs(transform.a * transform.e)

    def vol_of(lv: float) -> float:
        l2, _ = ndimage.label(work < lv)
        if not l2[row, col]:
            return 0.0
        p = l2 == l2[row, col]
        return float(np.sum(np.where(p, lv - work, 0.0))) * px_area

    levels, sills = equal_volume_levels(vol_of, z_min, wse, n_frames)
    grids = compute_lake_depth_grids(
        dem_array=dem, transform=transform, seed_xy=seed_xy, wse_m=wse,
        barrier_mask=barrier, barrier_crest_m=crest, levels_m=levels)
    for g in grids:
        g["binding"] = "TERRAIN"
        g["area_cap_m2"] = None
    ctx = {"transform": transform, "crs": crs, "dem": dem, "crest_m": crest,
           "z_min_m": z_min, "wse_m": wse, "sill_merges": sills,
           "barrier_cells": int(barrier.sum()),
           "crest_source": gr["crest_elev_source"],
           "crest_classification": gr["crest_elev_classification"]}
    return grids, ctx


# ── timing ────────────────────────────────────────────────────────────────────

def phutkal_times(fracs: list[float]) -> tuple[list[float], str, str]:
    """Sourced duration, assumed constant inflow.

    `formation_time_h` is in the scenario config and was, until now, read by
    nothing -- while the frames carried a hardcoded [-120, -60, -30, -10] min.
    The DURATION is sourced; the distribution of volume inside it is not, and
    the label has to say both.
    """
    lf = SCENARIOS["phutkal"].get("lake_formation") or {}
    hours = lf.get("formation_time_h")
    if not isinstance(hours, (int, float)) or not np.isfinite(hours):
        raise SystemExit(
            "phutkal.lake_formation.formation_time_h is required and absent — "
            "a fill duration is not defaulted, for the same reason crest_elev_m "
            "is not defaulted")
    total_min = float(hours) * 60.0
    # Constant inflow => volume grows linearly in time, so a frame holding
    # fraction f of the final volume sits at -T*(1-f).
    return ([-total_min * (1.0 - f) for f in fracs],
            "SOURCED_DURATION_ASSUMED_CONSTANT_INFLOW",
            f"duration {hours} h sourced from SCENARIOS['phutkal']['lake_formation']"
            f"['formation_time_h']; constant inflow assumed within it")


def annamayya_routed_rise() -> tuple[np.ndarray, np.ndarray]:
    """Pre-breach (t <= 0) reservoir elevation, routed from the Pincha surge."""
    from src.m3_breach.cascade import simulate_reservoir_cascade
    r = simulate_reservoir_cascade(SCENARIOS["annamayya"]["cascade"])
    pre = r.t_s <= 0.0
    return r.t_s[pre] / 60.0, r.reservoir_elevation_m[pre]


def annamayya_times(levels: list[float], t_pre: np.ndarray,
                    e_pre: np.ndarray) -> tuple[list[float], str, str]:
    """Routed where a routing exists; level-indexed where none does.

    The 2021 event is the band the cascade actually routes: the Pincha surge
    lifts the reservoir 203.60 -> 205.21 m over the 210 min before the breach,
    and those frames get their real time. Everything below 203.60 m is the
    antecedent impoundment, for which NO duration is sourced anywhere --
    `annamayya_event_evidence.json` carries the levels, volumes and discharges
    but no filling record. Those frames are indexed across a declared nominal
    window and say so; the alternative was to invent a fill rate, which is the
    defect this script exists to remove.

    The two bands are made to MEET at the routed start, so the animation has no
    discontinuity at the handover. The level range is capped at the routed peak
    by the caller, because above it the reservoir is holding water the routed
    event never gave it.
    """
    z_lo, t_lo = float(e_pre.min()), float(t_pre.min())
    nominal_min = 48.0 * 60.0          # declared, not sourced — see docstring
    times, routed = [], 0
    for lv in levels:
        if lv >= z_lo:
            times.append(float(np.interp(lv, e_pre, t_pre)))
            routed += 1
        else:
            frac = (lv - levels[0]) / max(z_lo - levels[0], 1e-9)
            times.append(t_lo - nominal_min * (1.0 - frac))
    return (times, "ROUTED_EVENT_BAND_PLUS_LEVEL_INDEXED_ANTECEDENT",
            f"{routed} of {len(levels)} frames routed by simulate_reservoir_cascade "
            f"over {z_lo:.2f}-{float(e_pre.max()):.2f} m ({abs(t_lo):.0f} min); the "
            f"rest are level-indexed across a declared {nominal_min/60:.0f} h nominal "
            f"window with NO sourced duration, joined at the routed start")


# ── writer ────────────────────────────────────────────────────────────────────

def write_run(key: str, frames: list[dict], times: list[float], transform, crs,
              out_dir: Path, extent_prov: str, time_prov: str, time_note: str,
              extras: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    snaps = out_dir / "snapshots"; snaps.mkdir(exist_ok=True)
    previews = out_dir / "preview_rasters"; previews.mkdir(exist_ok=True)
    to_wgs84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform

    v_full = max((f["volume_m3"] for f in frames), default=0.0) or 1.0
    stages = []
    for i, (f, t_min) in enumerate(zip(frames, times)):
        gj = snaps / f"frame_pre_{i:02d}.geojson"
        png = previews / f"frame_pre_{i:02d}.png"
        gj.write_text(json.dumps(_depth_to_geojson(
            f["depth_grid"], transform, t_min * 60.0, to_wgs84)), encoding="utf-8")
        bounds = _write_depth_preview(f["depth_grid"], png, transform, to_wgs84)
        pct = int(round(100.0 * f["volume_m3"] / v_full))
        stages.append({
            "t_s": t_min * 60.0,
            "t_min": round(t_min, 1),
            "stage": "lake_formation",
            "phase_title": f"Lake Formation ({pct}% Capacity, {f['level_m']:.1f} m)",
            "volume_mcm": round(f["volume_m3"] / 1e6, 2),
            "area_km2": round(f["area_m2"] / 1e6, 3),
            "level_m": round(f["level_m"], 2),
            "level_source": time_prov,
            "extent_provenance": extent_prov,
            "area_binding": f["binding"],
            "path": str(gj),
            "preview_path": str(png),
            "preview_bounds": bounds,
        })

    meta = {"scenario": key, "extent_provenance": extent_prov,
            "time_provenance": time_prov, "time_note": time_note,
            "frames": len(stages), **extras, "stages": stages}
    (out_dir / "lake_formation.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (out_dir / "snapshots_index.json").write_text(json.dumps(stages, indent=2), encoding="utf-8")
    return out_dir


# ── checks ────────────────────────────────────────────────────────────────────

def self_check(stages: list[dict], key: str) -> None:
    """The smallest set of assertions that fails if the frame logic breaks."""
    lv = [s["level_m"] for s in stages]
    vol = [s["volume_mcm"] for s in stages]
    area = [s["area_km2"] for s in stages]
    t = [s["t_min"] for s in stages]

    assert all(b > a for a, b in zip(lv, lv[1:])), f"levels not strictly increasing: {lv}"
    assert all(b >= a for a, b in zip(vol, vol[1:])), f"volume not monotone: {vol}"
    assert all(b >= a for a, b in zip(area, area[1:])), f"area not monotone: {area}"
    assert all(b > a for a, b in zip(t, t[1:])), f"time not increasing: {t}"
    # The defect this script exists to fix: no dead leading frames.
    assert all(a > 0 for a in area), f"empty frame(s): {area}"
    assert all(v > 0 for v in vol), f"zero-volume frame(s): {vol}"
    # The lake reaches capacity AT the breach, so the final frame legitimately
    # sits at T0 and hands straight off to the breach sequence. Every other
    # frame must be strictly before it. The old code dodged this by stopping the
    # fill at an arbitrary 95 %; there is no need to invent a cutoff.
    assert all(s["t_min"] <= 0 for s in stages), f"pre-breach frames must not follow T0: {t}"
    assert all(s["t_min"] < 0 for s in stages[:-1]), f"only the full-lake frame may sit at T0: {t}"

    # Spacing. A strict equal-increment assertion is UNSATISFIABLE and was
    # replaced rather than merely loosened: V(z) genuinely steps -- phutkal
    # gains 2.488 MCM in one 0.29 m probe step at 3762.76 m when the pool tops a
    # sill and takes in an adjacent basin, against ~0.24 MCM either side. No
    # level has a volume inside that gap, and the jump starves the frame after
    # it, so a sill disturbs THREE increments, not one. Measured on phutkal:
    # [0.74, 2.45, 0.20, then 1.13 x21].
    #
    # What must hold is that the animation is evenly paced EXCEPT where terrain
    # forces a step. Encoded as: the large majority of increments sit on the
    # median. Sloppy level selection spreads the increments out and still fails.
    d = np.diff([0.0] + vol)
    med = float(np.median(d))
    on_pace = float(np.mean(np.abs(d - med) <= 0.15 * med))
    assert on_pace >= 0.75, (
        f"only {on_pace:.0%} of increments are on the {med:.2f} MCM median — "
        f"level selection is uneven, not a sill: {list(np.round(d, 2))} MCM")
    print(f"  self-check PASSED for {key}: {len(stages)} frames, "
          f"volume {vol[0]:.2f} -> {vol[-1]:.2f} MCM, "
          f"area {area[0]:.3f} -> {area[-1]:.3f} km2, "
          f"{on_pace:.0%} of increments on the {med:.2f} MCM median "
          f"(largest {d.max():.2f} MCM at a sill)")


# ── main ──────────────────────────────────────────────────────────────────────

def build(key: str, n_frames: int, coarsen: int, out_root: Path) -> Path:
    if key == "phutkal":
        frames, ctx = dem_fill_frames(key, n_frames, coarsen)
        v_full = max(f["volume_m3"] for f in frames)
        times, tprov, tnote = phutkal_times([f["volume_m3"] / v_full for f in frames])
        extent_prov = "DEM_FILL_SOURCED_CREST"
        extras = {"crest_m": ctx["crest_m"], "crest_source": ctx["crest_source"],
                  "crest_classification": ctx["crest_classification"],
                  "barrier_cells": ctx["barrier_cells"],
                  "configured_volume_mcm": SCENARIOS[key]["volume_mcm"],
                  "dem_volume_mcm": round(v_full / 1e6, 2),
                  "volume_note": "the DEM-vs-configured gap is the GLO-30 gorge "
                                 "volume uncertainty the README states as +-10-20 %, "
                                 "carried here rather than hidden",
                  "sill_merges": ctx["sill_merges"]}
        transform, crs = ctx["transform"], ctx["crs"]

    elif key == "annamayya":
        with rasterio.open(DATA / "dem" / f"{key}_dem.tif") as src:
            z, transform, crs = src.read(1).astype(float), src.transform, src.crs
        s = SCENARIOS[key]
        res = s["cascade"]["reservoir"]
        fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
        bx, by = fwd(s["breach_lon"], s["breach_lat"])
        near_rc = rasterio.transform.rowcol(transform, bx, by)
        plane_m = float(z[near_rc])

        z_bed, z_frl = float(res["z_bed_m"]), float(res["z_frl_m"])
        v_frl, alpha = float(res["v_frl_mcm"]) * 1e6, float(res["alpha_exp"])

        def vol_of(lv: float) -> float:
            return v_frl * max((lv - z_bed) / (z_frl - z_bed), 0.0) ** alpha

        # Start at the observed plane, not at z_bed: everything below the plane
        # is hidden under the water the DSM captured, so its shape cannot be
        # reconstructed and is not guessed. Same refusal-to-extrapolate rule the
        # observed_wse layer already follows.
        #
        # Stop at the ROUTED PEAK, not at the crest. The surge lifts the
        # reservoir to 205.21 m and it breaches there — it never reaches the
        # 206.0 m bund top, so frames above the peak would show water the event
        # never held, and their times would all clamp to T0.
        t_pre, e_pre = annamayya_routed_rise()
        levels, sills = equal_volume_levels(
            lambda lv: vol_of(lv) - vol_of(plane_m), plane_m,
            float(e_pre.max()), n_frames)
        frames, transform = hypsometric_frames(key, levels, z, transform,
                                               plane_m, near_rc)
        times, tprov, tnote = annamayya_times(levels, t_pre, e_pre)
        extent_prov = "RECONSTRUCTED_HYPSOMETRIC"
        extras = {
            "plane_elev_m": plane_m,
            "stage_storage": {k: res[k] for k in
                              ("z_bed_m", "z_frl_m", "z_crest_m", "v_frl_mcm",
                               "alpha_exp", "classification")},
            "not_observed": ("the shoreline is RECONSTRUCTED, not observed. Area is "
                             "capped by the sourced stage-storage A(z)=dV/dz; shape "
                             "follows terrain from the DSM's captured water plane. "
                             "Never serve as the `observed` layer kind, never score "
                             "a CSI against it."),
            "anchor_check": "A(plane) analytic vs DSM plane area — see log line",
            "sill_merges": sills,
        }
    else:
        raise SystemExit(f"no lake-formation backend for {key!r} "
                         f"(have: phutkal, annamayya)")

    out = write_run(key, frames, times, transform, crs,
                    out_root / f"{key}_lake_formation",
                    extent_prov, tprov, tnote, extras)
    stages = json.loads((out / "snapshots_index.json").read_text(encoding="utf-8"))
    self_check(stages, key)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario", choices=["phutkal", "annamayya"])
    ap.add_argument("--frames", type=int, default=24)
    ap.add_argument("--coarsen", type=int, default=1, help="phutkal DEM fill only")
    ap.add_argument("--out", type=Path, default=DATA / "scenarios")
    ap.add_argument("--check", action="store_true",
                    help="build into a temp dir and assert, writing no run")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    warnings.filterwarnings("ignore")

    if a.check:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            build(a.scenario, a.frames, a.coarsen, Path(td))
        return

    out = build(a.scenario, a.frames, a.coarsen, a.out)
    meta = json.loads((out / "lake_formation.json").read_text(encoding="utf-8"))
    print(f"\n{out}")
    print(f"  extent : {meta['extent_provenance']}")
    print(f"  timing : {meta['time_provenance']}")
    print(f"           {meta['time_note']}")
    print(f"  {'t_min':>9} {'level_m':>9} {'vol_MCM':>9} {'area_km2':>9}  binding")
    for s in meta["stages"]:
        print(f"  {s['t_min']:>9.1f} {s['level_m']:>9.2f} {s['volume_mcm']:>9.2f} "
              f"{s['area_km2']:>9.3f}  {s['area_binding']}")


if __name__ == "__main__":
    main()
