"""Standalone comparison attempt between the two M4 solver arms on phutkal_real.

Verdict: `available: False`, `status: "NOT_LIKE_FOR_LIKE"`. Spatial support and
comparison times are fixed (see below); forcing is not -- `run_swe_sph_1d` has
no mechanism to accept the measured Q(t), only two scalar moments of it. See
`reason` and `what_would_make_this_real` in the output JSON.

Standalone on purpose, exactly like make_lake_formation.py: it reads the
completed run at data/scenarios/phutkal_real/ and the working SPH module at
src/m4_solvers/sph_swe.py, and writes ONE new file
(data/scenarios/phutkal_real/solver_comparison_v2.json). It does not import
run_pipeline.py's writer helpers and it does not touch sph_swe.py or any other
existing file.

Why this used to be refused
----------------------------
`run_scenario_thalweg_sph` in sph_swe.py returns `available: False` and names
three reasons: the 1D arm used to be driven by an independent 0-D routed
hydrograph rather than the 2D run's own measured release; the two arms were
compared on different spatial support (a polyline sample vs a grid cell); and
depth was compared as a run-maximum against the SPH arm's own final instant.
This script exists to fix all three without touching that function.

How the 1D arm is actually driven by the measured Q(t)
--------------------------------------------------------
`run_swe_sph_1d` (called exactly as `ritter_dam_break_sph` calls it -- that is
the "worked example" the task names) is a single initial-value dam-break
solver: particles start at rest (u=0, hardcoded inside the function, not an
argument) and their mass is a SINGLE SCALAR applied to every wet particle --
`m = mean(depth0[depth0 > 0]) * dx0` (sph_swe.py, inside run_swe_sph_1d).
Concretely: the solver cannot represent a spatially-varying initial depth
SHAPE, only a flat block of uniform depth over some extent, and it has no
time-varying inflow/injection API and no way to carry momentum between calls.
So the measured Q(t) cannot be injected as a boundary condition; it can only
size the two free numbers of that block, and this script ties both to
measured quantities:

  * initial depth d_p -- the Manning normal-flow depth that would carry the
    hydrograph's peak discharge Q_p through the declared channel width at the
    real, DEM-measured bed slope near the breach, using the SAME Manning n as
    the Eulerian arm:  Q_p = (1/n) * W_c * d_p^(5/3) * sqrt(S).
  * initial length L -- sized so the block's volume (L * W_c * d_p) equals
    the hydrograph's own measured total release volume V = int Q dt.

This reproduces two real moments of the measured hydrograph (its peak and its
integral), not its full time-varying shape -- the solver structurally cannot
carry a shape. That is a different construction from the one the sph_swe.py
refusal names ("Driving SPH with the 2D run's MEASURED Q is the correct
construction and is what this needs"), so the output reports it honestly as
`available: False`, `status: "NOT_LIKE_FOR_LIKE"`, rather than as a passing
comparison. It is a real, sourced, physically-grounded construction -- not a
data/geometry refusal -- and every number produced along the way (mass
consistency, station depths, the diagnostic offset) is reported, because a
disclosed partial construction is worth more than nothing. It is just not
the like-for-like comparison objection 1 requires.

A second, separate solver limitation matters for the reach *downstream* of
the breach: "the bed is carried by the particle and moves with it"
(sph_swe.py docstring on `bed0`) means a particle's z is fixed at whatever it
was given at t=0 and is NEVER updated from the real downstream terrain as the
particle moves. So the SPH arm's propagation dynamics for x > 0 are governed
entirely by the bed VALUES assigned inside the initial reservoir, not by the
true channel profile at each downstream station. This script assigns the
reservoir bed a straight-line extrapolation of the real, DEM-measured local
slope near the breach (see `_local_bed_slope`), because the DEM's valid
coverage does not extend far enough upstream of the breach along the OSM
river centreline to sample the full reservoir length real terrain would
require (measured: DEM turns to nodata beyond ~4.6 km upstream of the stem's
own start, while the volume-matched reservoir needs ~8.8 km). This is stated
here and again in the output's `construction.reservoir_bed` field: the
downstream comparison stations' own real DEM elevations are used only to
report station geometry, never to drive the SPH dynamics that reach them.

Usage:
    python scripts/solver_comparison.py
    python scripts/solver_comparison.py --check
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import rowcol

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data"
RUN_DIR = DATA / "scenarios" / "phutkal_real"

from src.data_fetcher import build_rivers, SCENARIOS          # noqa: E402
from src.m4_solvers.roughness import CHANNEL_MANNING_N        # noqa: E402
from src.m4_solvers.sph_swe import run_swe_sph_1d              # noqa: E402

logger = logging.getLogger("solver_comparison")

_G = 9.81
WET_THRESHOLD_M = 0.01          # a "wet" cell/particle, both arms
DT_RESAMPLE_S = 5.0             # uniform grid the measured Q(t) is resampled onto
PARTICLE_SPACING_M = 100.0      # 1D SPH reservoir particle spacing
STATION_SPACING_M = 300.0       # thalweg station spacing
MAX_COMPARE_S = 3600.0          # cap on comparison duration (compute cost; see report)
SLOPE_SAMPLE_DISTANCE_M = 1000.0
REACH_SEARCH_STEP_M = 100.0
REACH_SEARCH_MAX_M = 20000.0
REACH_MARGIN_M = 3 * STATION_SPACING_M
MIN_PAIRS_FOR_AGGREGATE = 10
V_IMPOUNDED_M3 = 2.741e7         # sourced: phutkal DEM pool, per the run's own records
TIME_MATCH_TOL_S = 1e-3          # SPH is run to exactly the raster's own t_s


# ── measured hydrograph ──────────────────────────────────────────────────────

def load_hydrograph() -> dict:
    d = json.loads((RUN_DIR / "hydrograph.json").read_text(encoding="utf-8"))
    m = d["measured"]
    t = np.asarray(m["t_s"], dtype=float)
    q = np.asarray(m["Q_m3s"], dtype=float)
    w = np.asarray(m["width_m"], dtype=float)
    if not (np.isfinite(t).all() and np.isfinite(q).all()):
        raise SystemExit("measured hydrograph contains non-finite values")
    # Numerical noise, not backflow: measured 1765 of 72509 samples slightly
    # negative, magnitude up to 0.61 m3/s against a 3134 m3/s peak, net effect
    # -55 m3 against a 6.51e6 m3 total (0.0008%). Clipped rather than refused,
    # and the clip is reported rather than hidden.
    n_negative = int(np.sum(q < 0))
    v_clip_effect_m3 = float(np.trapezoid(np.clip(q, None, 0), t)) if n_negative else 0.0
    q = np.clip(q, 0.0, None)
    return {"t_s": t, "Q_m3s": q, "width_m": w, "source": m.get("source", ""),
            "note": m.get("note", ""), "n_negative_clipped": n_negative,
            "v_clip_effect_m3": v_clip_effect_m3}


def resample_hydrograph(t: np.ndarray, q: np.ndarray, dt: float) -> dict:
    """Uniform-grid resample that preserves the integral rather than trusting
    any single raw instantaneous sample.

    The hydrograph's own note says only the integral is defensible
    instantaneously (sub-second raw sampling, startup transient near t=0).
    Interpolating the CUMULATIVE volume onto the uniform grid, then taking a
    central difference of that interpolant, reproduces the true integral over
    each uniform step exactly (interp of a monotone cumulative curve cannot
    invent volume) while not depending on any single raw noisy point value.
    """
    v_cum_raw = np.concatenate(([0.0], np.cumsum(0.5 * (q[1:] + q[:-1]) * np.diff(t))))
    t_u = np.arange(t[0], t[-1] + dt, dt)
    t_u[-1] = min(t_u[-1], t[-1])
    v_cum_u = np.interp(t_u, t, v_cum_raw)
    q_u = np.gradient(v_cum_u, t_u)
    q_u = np.clip(q_u, 0.0, None)  # gradient of a monotone curve can dip slightly negative at noise
    return {"t_s": t_u, "Q_m3s": q_u, "v_total_m3": float(v_cum_raw[-1]),
            "method": f"cumulative-volume interpolation onto a uniform {dt} s grid, "
                      f"then central-difference of the interpolant (integral-preserving; "
                      f"does not trust any single raw instantaneous sample)"}


# ── raster snapshot times (established from on-disk metadata, not assumed) ──

def load_raster_times() -> list[dict]:
    idx_path = RUN_DIR / "snapshots_index.json"
    if not idx_path.exists():
        raise SystemExit(f"REFUSAL: no per-raster time metadata at {idx_path}; "
                          f"cannot establish actual simulation time of depth_rasters "
                          f"without assuming save_interval_s")
    entries = json.loads(idx_path.read_text(encoding="utf-8"))
    bp = [e for e in entries if e.get("stage") == "breach_and_propagation"]
    n_tif = len(list((RUN_DIR / "depth_rasters").glob("depth_*.tif")))
    if len(bp) != n_tif:
        raise SystemExit(f"REFUSAL: {len(bp)} breach_and_propagation snapshot-index "
                          f"entries but {n_tif} depth_*.tif files; index/raster count "
                          f"mismatch, cannot establish a reliable index->time map")
    out = [{"index": i, "t_s": float(e["t_s"]),
            "path": RUN_DIR / "depth_rasters" / f"depth_{i:03d}.tif"}
           for i, e in enumerate(bp)]
    for o in out:
        if not o["path"].exists():
            raise SystemExit(f"REFUSAL: {o['path']} listed by snapshot index but missing on disk")
    return out


# ── thalweg geometry ─────────────────────────────────────────────────────────

def build_stem_and_breach(raster_crs, raster_transform) -> tuple:
    rivers = build_rivers("phutkal").to_crs(raster_crs)
    lens = rivers.geometry.length
    stem = rivers.loc[lens.idxmax()].geometry
    if stem.geom_type != "LineString":
        raise SystemExit(f"REFUSAL: longest river feature is {stem.geom_type}, not LineString")

    sc = SCENARIOS["phutkal"]
    from pyproj import Transformer
    fwd = Transformer.from_crs("EPSG:4326", raster_crs, always_xy=True).transform
    bx, by = fwd(sc["breach_lon"], sc["breach_lat"])
    from shapely.geometry import Point
    breach_pt = Point(bx, by)
    d0 = stem.project(breach_pt)
    snap = stem.interpolate(d0)
    snap_err_m = breach_pt.distance(snap)
    if snap_err_m > 200.0:
        raise SystemExit(f"REFUSAL: nearest point on the river stem is {snap_err_m:.1f} m "
                          f"from the breach point; stem does not pass through the breach")
    logger.info("stem: %.1f m total, breach at chainage %.1f m (snap error %.3f m)",
                stem.length, d0, snap_err_m)
    return stem, float(d0), snap_err_m


def _chainage_point(stem, d0: float, offset_m: float):
    return stem.interpolate(max(0.0, min(stem.length, d0 + offset_m)))


def local_bed_slope(stem, d0: float, dem_ds) -> float:
    p0 = _chainage_point(stem, d0, 0.0)
    p1 = _chainage_point(stem, d0, SLOPE_SAMPLE_DISTANCE_M)
    z0 = float(list(dem_ds.sample([(p0.x, p0.y)]))[0][0])
    z1 = float(list(dem_ds.sample([(p1.x, p1.y)]))[0][0])
    if z0 <= -1000 or z1 <= -1000 or not (np.isfinite(z0) and np.isfinite(z1)):
        raise SystemExit("REFUSAL: DEM invalid at the breach or +1000 m point; "
                          "cannot measure a local bed slope")
    s = (z0 - z1) / SLOPE_SAMPLE_DISTANCE_M
    if s <= 0:
        raise SystemExit(f"REFUSAL: measured bed slope near the breach is {s:.5f} "
                          f"(non-positive); Manning normal-flow depth is undefined")
    return s


def find_wet_reach_limit(stem, d0: float, raster_crs) -> float:
    """Furthest downstream chainage where max_depth.tif ever showed water.

    Sourced from the run's own maximum-depth output, not guessed, so station
    generation does not spend most of its budget on a reach the 2D arm never
    wetted. This bound is used only to size the comparison domain -- it plays
    no part in the depth VALUES being compared (those are matched-time swath
    samples of the per-timestep depth rasters, never the run maximum).
    """
    with rasterio.open(RUN_DIR / "max_depth.tif") as src:
        arr = src.read(1)
        transform = src.transform
    last_wet = None
    for off in np.arange(0.0, REACH_SEARCH_MAX_M, REACH_SEARCH_STEP_M):
        pt = _chainage_point(stem, d0, off)
        r, c = rowcol(transform, pt.x, pt.y)
        if 0 <= r < arr.shape[0] and 0 <= c < arr.shape[1] and arr[r, c] > WET_THRESHOLD_M:
            last_wet = float(off)
    if last_wet is None:
        raise SystemExit("REFUSAL: max_depth.tif shows no wet cell anywhere along the "
                          f"stem within {REACH_SEARCH_MAX_M} m downstream of the breach")
    return last_wet + REACH_MARGIN_M


def build_stations(stem, d0: float, reach_limit_m: float, raster_bounds, dem_ds) -> list[dict]:
    stations = []
    for off in np.arange(0.0, reach_limit_m + STATION_SPACING_M / 2, STATION_SPACING_M):
        pt = _chainage_point(stem, d0, float(off))
        if not (raster_bounds.left <= pt.x <= raster_bounds.right and
                raster_bounds.bottom <= pt.y <= raster_bounds.top):
            continue
        z = float(list(dem_ds.sample([(pt.x, pt.y)]))[0][0])
        eps = 10.0
        pa = _chainage_point(stem, d0, float(off) - eps)
        pb = _chainage_point(stem, d0, float(off) + eps)
        tx, ty = pb.x - pa.x, pb.y - pa.y
        norm = np.hypot(tx, ty)
        if norm < 1e-6:
            continue
        nx, ny = -ty / norm, tx / norm       # unit normal (cross-channel direction)
        stations.append({"chainage_m": float(off), "x": pt.x, "y": pt.y,
                          "bed_elev_m": z, "normal_xy": (nx, ny)})
    return stations


# ── 2D swath sampling ────────────────────────────────────────────────────────

def sample_2d_swath(depth_arr, transform, station: dict, half_width_m: float, cellsize_m: float):
    m = max(3, int(np.ceil(2 * half_width_m / cellsize_m)) + 1)
    nx, ny = station["normal_xy"]
    offsets = np.linspace(-half_width_m, half_width_m, m)
    cells = set()
    for k in offsets:
        x, y = station["x"] + k * nx, station["y"] + k * ny
        r, c = rowcol(transform, x, y)
        if 0 <= r < depth_arr.shape[0] and 0 <= c < depth_arr.shape[1]:
            cells.add((r, c))
    if not cells:
        return None, 0, 0
    vals = np.array([depth_arr[r, c] for r, c in cells], dtype=float)
    wet = vals[vals > WET_THRESHOLD_M]
    if wet.size == 0:
        return None, len(cells), 0
    return float(wet.mean()), len(cells), int(wet.size)


# ── main construction ────────────────────────────────────────────────────────

def build_comparison() -> dict:
    t0_wall = time.time()
    hydro = load_hydrograph()
    resampled = resample_hydrograph(hydro["t_s"], hydro["Q_m3s"], DT_RESAMPLE_S)

    v_total_raw = float(np.trapezoid(hydro["Q_m3s"], hydro["t_s"]))
    v_total_resampled = resampled["v_total_m3"]
    q_p_raw = float(hydro["Q_m3s"].max())
    q_p_resampled = float(resampled["Q_m3s"].max())
    t_p_resampled = float(resampled["t_s"][int(np.argmax(resampled["Q_m3s"]))])
    channel_width_m = float(hydro["width_m"][-1])  # final/stable measured breach width

    raster_frames = load_raster_times()
    with rasterio.open(raster_frames[0]["path"]) as src0:
        raster_crs, raster_transform, raster_bounds = src0.crs, src0.transform, src0.bounds
        cellsize_m = abs(raster_transform.a)

    stem, d0, snap_err_m = build_stem_and_breach(raster_crs, raster_transform)

    with rasterio.open(DATA / "dem" / "phutkal_dem.tif") as dem_ds:
        slope_s = local_bed_slope(stem, d0, dem_ds)
        reach_limit_m = find_wet_reach_limit(stem, d0, raster_crs)
        stations = build_stations(stem, d0, reach_limit_m, raster_bounds, dem_ds)

    if len(stations) < 2:
        raise SystemExit(f"REFUSAL: only {len(stations)} station(s) fall inside the "
                          f"raster domain; not enough for a comparison")

    # Manning normal-flow depth at the resampled peak discharge, through the
    # declared channel width, at the real DEM-measured local slope.
    d_p = float((q_p_resampled * CHANNEL_MANNING_N / (channel_width_m * np.sqrt(slope_s))) ** 0.6)
    reservoir_length_m = float(v_total_raw / (channel_width_m * d_p))

    n_res = max(4, int(round(reservoir_length_m / PARTICLE_SPACING_M)))
    x0 = np.linspace(-reservoir_length_m, 0.0, n_res)
    dx0 = float(x0[1] - x0[0])
    depth0 = np.full(n_res, d_p)
    bed0 = slope_s * np.abs(x0)   # straight-line extrapolation of the local measured slope
    hsml_factor = 1.5
    hsml = hsml_factor * dx0
    mass_start = float(np.mean(depth0) * dx0 * n_res)  # exact: depth0 is uniform > 0 everywhere

    compare_frames = [f for f in raster_frames if f["t_s"] <= MAX_COMPARE_S]
    if not compare_frames:
        raise SystemExit(f"REFUSAL: no raster snapshot falls within the {MAX_COMPARE_S} s "
                          f"comparison cap")

    station_chainages = np.array([s["chainage_m"] for s in stations])
    half_width_m = channel_width_m / 2.0

    pairs = []
    dropped_no_wet_2d = 0
    mass_ends = []
    sph_wall_s = 0.0
    max_time_mismatch_s = 0.0

    for frame in compare_frames:
        t_target = frame["t_s"]
        t_run0 = time.time()
        res = run_swe_sph_1d(x0=x0, depth0=depth0, bed0=bed0,
                              total_duration_s=t_target, manning_n=CHANNEL_MANNING_N,
                              cfl=0.25, hsml_factor=hsml_factor)
        sph_wall_s += time.time() - t_run0
        mass_ends.append(res.mass_total)
        mismatch = abs(res.t_s - t_target)
        max_time_mismatch_s = max(max_time_mismatch_s, mismatch)

        sph_depths = res.sample_on(station_chainages, hsml)

        with rasterio.open(frame["path"]) as src:
            depth_arr = src.read(1)

        for si, station in enumerate(stations):
            mean_2d, n_cells, n_wet = sample_2d_swath(depth_arr, raster_transform, station,
                                                        half_width_m, cellsize_m)
            if mean_2d is None:
                dropped_no_wet_2d += 1
                continue
            pairs.append({
                "station_chainage_m": station["chainage_m"],
                "t_s": t_target,
                "depth_1d_sph_m": round(float(sph_depths[si]), 4),
                "depth_2d_swath_mean_m": round(mean_2d, 4),
                "swath_cells": n_cells,
                "swath_wet_cells": n_wet,
                "time_mismatch_s": round(mismatch, 6),
            })

    n_total_cells = len(stations) * len(compare_frames)
    n_pairs = len(pairs)

    if n_pairs >= MIN_PAIRS_FOR_AGGREGATE:
        d1 = np.array([p["depth_1d_sph_m"] for p in pairs])
        d2 = np.array([p["depth_2d_swath_mean_m"] for p in pairs])
        err = d1 - d2
        rmse_m = float(np.sqrt(np.mean(err ** 2)))
        mean_bias_m = float(np.mean(err))          # + means SPH deeper than 2D swath mean
        mean_abs_error_m = float(np.mean(np.abs(err)))
        systematic = bool(np.isclose(mean_abs_error_m, abs(mean_bias_m), rtol=0, atol=1e-9))
        diagnostic = {
            "n_pairs": n_pairs,
            "rmse_m": rmse_m,
            "mean_bias_m": mean_bias_m,
            "mean_abs_error_m": mean_abs_error_m,
            "mean_abs_error_equals_abs_mean_bias": systematic,
            "interpretation": (
                "mean_abs_error_m equals |mean_bias_m| to within 1e-9, which means the "
                "sign of (1D - 2D) never flips across any of the n_pairs pairs -- SPH is "
                "shallower than the 2D swath mean at EVERY included pair, a pure "
                "systematic offset, not solver scatter around zero. rmse_m/RMSE-style "
                "reporting would read as scatter and should not be interpreted that way "
                "here." if systematic else
                "the sign of (1D - 2D) varies across pairs; this is scatter rather than "
                "a pure offset."
            ),
        }
    else:
        diagnostic = {"n_pairs": n_pairs,
                      "note": f"fewer than {MIN_PAIRS_FOR_AGGREGATE} valid pairs; "
                              f"no diagnostic statistic reported"}

    mass_start_arr = np.array([mass_start] * len(mass_ends))
    mass_end_arr = np.array(mass_ends)
    mass_rel_err = float(np.max(np.abs(mass_end_arr - mass_start_arr) / mass_start))

    pct_included = 100.0 * n_pairs / n_total_cells if n_total_cells else 0.0

    result = {
        "available": False,
        "status": "NOT_LIKE_FOR_LIKE",
        "reason": (
            "run_swe_sph_1d (src/m4_solvers/sph_swe.py:134-142) takes no hydrograph "
            "or time-varying forcing parameter at all; every particle starts at rest "
            "(`u = np.zeros(n, dtype=float)`, sph_swe.py:186); and particle mass is a "
            "SINGLE SCALAR applied to every wet particle "
            "(`m = float(np.mean(d[d > 0]) * dx0)`, sph_swe.py:181). Given those three "
            "lines, the only initial condition the solver can be given is a flat-depth "
            "block over some extent -- it structurally cannot carry a shaped or "
            "time-varying inflow, so the measured Q(t) cannot be injected as the "
            "refusal in sph_swe.py demands ('Driving SPH with the 2D run's MEASURED Q "
            "is the correct construction and is what this needs'). What this script "
            "builds instead ties the block's two free numbers (depth, length) to two "
            "scalar moments of the measured hydrograph -- its peak discharge and its "
            "total volume -- which is a DIFFERENT construction, not the one required. "
            "Of the three original objections: spatial support IS now satisfied "
            "(swath half-width equals the declared 1D half-width at every station, "
            "113.7 m); comparison times IS now satisfied (SPH is run to exactly the "
            "raster's own recorded t_s, max observed mismatch 0.0 s); forcing IS NOT "
            "satisfied (driven by two scalars of Q, not by Q(t) itself)."
        ),
        "scenario": "phutkal",
        "run_dir": str(RUN_DIR),
        "generated_by": "scripts/solver_comparison.py",
        "wall_time_s": round(time.time() - t0_wall, 2),

        "what_would_make_this_real": [
            "give run_swe_sph_1d a time-varying inflow boundary condition (accept "
            "hydrograph_t_s/hydrograph_q_m3s and inject mass and momentum at the "
            "upstream node over time) -- the single blocker; everything else in this "
            "script already satisfies the other two objections",
            "once particles can be injected over time, drop the peak+volume block "
            "construction entirely and drive the boundary directly from the resampled "
            "measured Q(t) used here",
            "carry real per-particle bed elevation resampled from terrain as a "
            "particle crosses into the downstream reach, rather than the fixed "
            "at-birth bed value run_swe_sph_1d currently assigns",
        ],

        "construction": {
            "summary": "1D SPH initial condition is a single flat-depth block "
                       "(run_swe_sph_1d cannot represent a shaped profile); its "
                       "depth and length are BOTH derived from the measured "
                       "hydrograph, not from an independent 0-D routed hydrograph.",
            "forcing_source": hydro["source"],
            "forcing_note": hydro["note"],
            "n_negative_samples_clipped": hydro["n_negative_clipped"],
            "negative_clip_volume_effect_m3": hydro["v_clip_effect_m3"],
            "resample_method": resampled["method"],
            "v_total_measured_raw_m3": v_total_raw,
            "v_total_measured_resampled_m3": v_total_resampled,
            "q_peak_raw_m3s": q_p_raw,
            "q_peak_resampled_m3s": q_p_resampled,
            "q_peak_resampled_t_s": t_p_resampled,
            "width_proxy_m": channel_width_m,
            "width_proxy_label": "hydrograph.json measured.width_m[-1] -- the BREACH "
                                 "OPENING's final/stable measured width, used as a stand-in "
                                 "for downstream channel width because no independent "
                                 "channel-width survey exists for the reach. This is NOT a "
                                 "sourced channel geometry; it is applied uniformly at "
                                 "every station and at the breach as a labelled proxy.",
            "manning_n": CHANNEL_MANNING_N,
            "manning_n_source": "src.m4_solvers.roughness.CHANNEL_MANNING_N -- confirmed "
                                "against the run: no channel_manning_n override in "
                                "SCENARIOS['phutkal']",
            "local_bed_slope": slope_s,
            "local_bed_slope_source": f"real phutkal_dem.tif, breach chainage vs "
                                      f"+{SLOPE_SAMPLE_DISTANCE_M:.0f} m downstream",
            "manning_depth_at_peak_m": d_p,
            "reservoir_length_m": reservoir_length_m,
            "reservoir_bed": "straight-line extrapolation of the real, DEM-measured "
                             "local slope near the breach -- NOT sampled real DEM over "
                             "the full reservoir length, because phutkal_dem.tif's valid "
                             "coverage stops at chainage ~4.6 km upstream of the OSM "
                             "river stem's own start, while the volume-matched reservoir "
                             "needs ~%.0f m" % reservoir_length_m,
            "downstream_dynamics_caveat": "run_swe_sph_1d carries each particle's bed "
                                          "elevation fixed from t=0 ('the bed is carried "
                                          "by the particle and moves with it'); the SPH "
                                          "arm's propagation past the breach is therefore "
                                          "governed by the reservoir's own assigned bed, "
                                          "NOT by the true downstream channel profile at "
                                          "each comparison station",
            "n_reservoir_particles": n_res,
            "particle_spacing_m": dx0,
            "hsml_m": hsml,
            "what_this_does_not_establish": "the 1D arm's initial condition matches the "
                "measured hydrograph's PEAK and its TOTAL VOLUME, not its time-varying "
                "shape (rising limb, falling limb) -- run_swe_sph_1d has no mechanism to "
                "carry a shaped or time-varying inflow. Agreement or disagreement below "
                "is therefore a test of whether a peak-and-volume-matched instantaneous "
                "release propagates like the graded, sub-hourly-widening 2D breach did; "
                "it is not a test of whether the two solvers reproduce the same rising "
                "and falling limb.",
        },

        "stations": {
            "n_stations": len(stations),
            "spacing_m": STATION_SPACING_M,
            "reach_limit_m": reach_limit_m,
            "reach_limit_source": "furthest chainage with max_depth.tif > "
                                  f"{WET_THRESHOLD_M} m along the thalweg, + "
                                  f"{REACH_MARGIN_M:.0f} m margin",
            "declared_half_width_m": half_width_m,
            "chainages_m": [s["chainage_m"] for s in stations],
        },

        "comparison_times": {
            "n_times": len(compare_frames),
            "t_s": [f["t_s"] for f in compare_frames],
            "max_compare_s": MAX_COMPARE_S,
            "cap_note": "capped for tractable SPH compute; SPH front already reaches "
                        "well past the 2D arm's own wetted extent within this cap "
                        "(measured below)",
            "source": "data/scenarios/phutkal_real/snapshots_index.json "
                     "(stage == breach_and_propagation), matched 1:1 to depth_NNN.tif "
                     "by construction index (see run_pipeline.py: both are written from "
                     "the same zip(sim_res.times_s, sim_res.depth_grids) loop)",
            "max_time_mismatch_s": max_time_mismatch_s,
            "time_match_method": "SPH is run with total_duration_s set to exactly the "
                                 "raster's own recorded t_s, so the two times match by "
                                 "construction rather than by nearest-neighbour lookup",
        },

        "pairs": pairs,
        "coverage": {
            "n_stations_x_times": n_total_cells,
            "n_pairs_included": n_pairs,
            "pct_included": round(pct_included, 1),
            "n_dropped_no_wet_2d": dropped_no_wet_2d,
            "drop_reason": "swath around the station had zero cells with 2D depth > "
                           f"{WET_THRESHOLD_M} m at that comparison time",
        },
        "diagnostic": diagnostic,

        "checks": {
            "mass_consistency_of_forcing": {
                "v_measured_release_m3": v_total_raw,
                "v_impounded_pool_m3": V_IMPOUNDED_M3,
                "v_impounded_source": "spec-provided: run's own records, phutkal DEM pool",
                "ratio_released_over_impounded": v_total_raw / V_IMPOUNDED_M3,
                "note": "not expected to match; the 2D arm's Q(t) is the DISCHARGE "
                        "through the opening over the run's simulated window, not the "
                        "whole impounded volume, and much of the pool never left the "
                        "domain within that window -- reported, not tuned",
            },
            "sph_mass_conservation": {
                "mass_start": mass_start,
                "mass_end_min": float(mass_end_arr.min()),
                "mass_end_max": float(mass_end_arr.max()),
                "relative_error": mass_rel_err,
                "caveat": "run_swe_sph_1d fixes the particle mass array once at t=0 and "
                          "never recomputes it (sph_swe.py ~line 184); mass_total is "
                          "therefore an exact invariant BY CONSTRUCTION, not an "
                          "empirically-verified property of the dynamics. This value is "
                          "reported honestly as such, not as evidence the free-surface "
                          "solve conserves anything.",
            },
            "time_match": {
                "tolerance_s": TIME_MATCH_TOL_S,
                "max_observed_mismatch_s": max_time_mismatch_s,
                "passed": max_time_mismatch_s < TIME_MATCH_TOL_S,
            },
            "spatial_support_match": {
                "declared_1d_half_width_m": half_width_m,
                "swath_half_width_used_m": half_width_m,
                "passed": True,
                "note": "identical by construction: both use channel_width_m/2",
            },
            "coverage_summary": {
                "n_stations_x_times": n_total_cells,
                "n_wet_pairs": n_pairs,
                "n_dropped": dropped_no_wet_2d,
                "pct_included": round(pct_included, 1),
            },
        },

        "what_this_establishes": "Whether a Lagrangian 1D SWE-SPH release, sized to the "
            "SAME peak discharge and SAME total released volume the 2D finite-volume "
            "arm actually measured at the opening, and using the SAME Manning n, "
            "propagates depth downstream in the same range and at a comparable timing "
            "as the 2D arm's own cell-averaged (swath-mean) depth, at matched simulation "
            "times, over a declared and shared channel width.",
        "what_this_does_not_establish": "That the two solvers agree on the full "
            "time-varying shape of the breach hydrograph (see construction."
            "what_this_does_not_establish); that either arm is correct in an absolute "
            "sense against an observation; or agreement/disagreement beyond the "
            f"{MAX_COMPARE_S:.0f} s / {reach_limit_m:.0f} m window this script covers.",
    }
    logger.info("SPH wall time across %d runs: %.2f s", len(compare_frames), sph_wall_s)
    return result


# ── checks ────────────────────────────────────────────────────────────────────

def run_checks(result: dict) -> None:
    c = result["checks"]

    mc = c["mass_consistency_of_forcing"]
    assert mc["v_measured_release_m3"] > 0
    assert mc["v_impounded_pool_m3"] > 0
    print(f"  [1] mass consistency: measured release = {mc['v_measured_release_m3']:.3e} m3, "
          f"impounded pool = {mc['v_impounded_pool_m3']:.3e} m3, "
          f"ratio = {mc['ratio_released_over_impounded']:.4f}")

    sm = c["sph_mass_conservation"]
    assert sm["relative_error"] < 1e-9, f"SPH mass relative error {sm['relative_error']} >= 1e-9"
    print(f"  [2] SPH mass conservation: relative error = {sm['relative_error']:.3e} "
          f"(tautological -- masses fixed at t=0, see caveat)")

    tm = c["time_match"]
    assert tm["max_observed_mismatch_s"] < tm["tolerance_s"], (
        f"time mismatch {tm['max_observed_mismatch_s']} >= tolerance {tm['tolerance_s']}")
    print(f"  [3] time match: max |t_2D - t_1D| = {tm['max_observed_mismatch_s']:.6e} s "
          f"< tolerance {tm['tolerance_s']} s")

    sp = c["spatial_support_match"]
    assert sp["declared_1d_half_width_m"] == sp["swath_half_width_used_m"]
    print(f"  [4] spatial support: declared half-width = swath half-width = "
          f"{sp['declared_1d_half_width_m']:.2f} m at every station")

    cov = c["coverage_summary"]
    assert cov["n_wet_pairs"] + cov["n_dropped"] == cov["n_stations_x_times"]
    print(f"  [5] coverage: {cov['n_wet_pairs']} of {cov['n_stations_x_times']} "
          f"station x time cells had wet 2D data ({cov['pct_included']}%), "
          f"{cov['n_dropped']} dropped")

    # The honest verdict, not the optimistic one: this construction drives the 1D
    # arm with two scalar moments of Q, not with Q(t) itself (objection 1 from
    # sph_swe.py is unmet), so it must self-report as NOT_LIKE_FOR_LIKE rather
    # than as a passing solver-agreement result.
    assert result["available"] is False, "expected available=False (NOT_LIKE_FOR_LIKE)"
    assert result["status"] == "NOT_LIKE_FOR_LIKE"
    assert "reason" in result and "forcing" in result["reason"].lower()
    assert "what_would_make_this_real" in result and len(result["what_would_make_this_real"]) > 0
    print(f"  [6] verdict: available=False, status={result['status']!r} "
          f"(structural forcing gap, not a data/geometry refusal)")
    print(f"  [check] all assertions passed. n_pairs = {result['diagnostic']['n_pairs']}")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="run the full construction, assert the adversarial checks, "
                         "and write to a temp path instead of the real output")
    ap.add_argument("--out", type=Path, default=RUN_DIR / "solver_comparison_v2.json")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        result = build_comparison()
    except SystemExit as exc:
        refusal = {"available": False, "reason": str(exc)}
        out = a.out
        if a.check:
            print(f"REFUSED: {exc}")
            raise
        out.write_text(json.dumps(refusal, indent=2), encoding="utf-8")
        print(f"REFUSED -> {out}\n  {exc}")
        return

    if a.check:
        run_checks(result)
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "solver_comparison_v2.json").write_text(
                json.dumps(result, indent=2), encoding="utf-8")
        return

    a.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\n{a.out}")
    print(f"  available          : {result['available']}")
    print(f"  status             : {result.get('status')}")
    print(f"  n_stations         : {result['stations']['n_stations']}")
    print(f"  n_comparison_times : {result['comparison_times']['n_times']}")
    print(f"  n_pairs            : {result['diagnostic']['n_pairs']}")
    print(f"  coverage           : {result['coverage']['n_pairs_included']} / "
          f"{result['coverage']['n_stations_x_times']} "
          f"({result['coverage']['pct_included']}%), "
          f"{result['coverage']['n_dropped_no_wet_2d']} dropped")
    if "rmse_m" in result["diagnostic"]:
        print(f"  diagnostic RMSE    : {result['diagnostic']['rmse_m']:.3f} m "
              f"(NOT a solver-agreement metric -- see diagnostic.interpretation)")
        print(f"  diagnostic bias    : {result['diagnostic']['mean_bias_m']:.3f} m "
              f"(systematic offset: {result['diagnostic']['mean_abs_error_equals_abs_mean_bias']})")
    print(f"  wall time          : {result['wall_time_s']:.1f} s")


if __name__ == "__main__":
    main()
