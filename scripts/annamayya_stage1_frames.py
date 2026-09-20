"""Stage 1 for Annamayya: the sourced pre-breach reservoir rise.

The problem statement asks for lake formation AND the breach. The served
annamayya run had only the breach half -- 97 `stage: "routing"` frames starting
at T+0 -- because the 2D cascade Stage 1 failed its arrival gate (+12,601 s
against a [-5400, -2400] s window, findings_results.md) and was dropped.

This module supplies the missing half the same way the rest of that run is
supplied: **prescribed from sourced figures and labelled as such**. The run's
own manifest already says `FORCED-HYDROGRAPH ROUTE ... discharge is an input
here, not a measured output`; the stage curve below is the same class of thing
for level, and carries the same warning.

What is sourced, and what is not
--------------------------------
SOURCED (data/evidence/annamayya_event_evidence.json):
  * FRL 203.6 m MSL                         OBSERVED
  * bund top / crest 206.0 m MSL            OBSERVED
  * Pincha ring-bund washout at T-10800 s   OBSERVED       (event_clock)
  * overtopping initiation at T-2700 s      OFFICIAL_ESTIMATE (event_clock)
  * Pincha surge peak 3,964.4 m3/s          OFFICIAL_ESTIMATE
  * Pincha->Annamayya travel 120 min        MODEL_RECONSTRUCTION
  * Pincha storage 9.28 MCM (surcharge 14)  OFFICIAL_ESTIMATE

DERIVED HERE:
  * A(z), V(z) for the reservoir, measured off the same GLO-30 the run uses.
  * z(t), by integrating dV = Q dt from FRL at T-10800 s.

NOT CLAIMED: this is not a solver result, and the impoundment is not modelled.
GLO-30 over this AOI is a DSM captured with the reservoir full -- a flat
192.50 m water plane against a published 206.0 m crest -- so there is no
bathymetry beneath the pool and A(z)/V(z) below 192.50 m do not exist. Every
volume here is therefore volume ABOVE THE OBSERVED WATER SURFACE, not gross
storage, and the frames say so. See memory.md, "The Annamayya dam is not in the
DEM".

The crossing time is REPORTED, NEVER TUNED. If the integrated curve does not
reach 206.0 m at the sourced T-2700 s, that gap is the finding and it is
printed and carried onto the frames. Nothing here adjusts an inflow to close it.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import rasterio
from scipy import ndimage

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "data" / "evidence" / "annamayya_event_evidence.json"

# The DSM's reservoir water plane. Everything at or below this is the observed
# pool surface, not terrain, so it is the floor of every volume computed here.
DSM_POOL_PLANE_M = 192.50
_PLANE_TOL_M = 0.30          # the plane is flat to ~0.1 m; this admits its edge


def sourced() -> dict:
    """Pull the Stage-1 figures out of the evidence file, with provenance."""
    ev = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    p, clock = ev["parameters"], ev["event_clock"]
    events = {e["id"]: e for e in clock["timeline_events"]}
    return {
        "frl_m": float(p["annamayya_frl_m"]["value"]),
        "frl_src": p["annamayya_frl_m"]["source"],
        "crest_m": float(p["annamayya_bund_top_level_m"]["value"]),
        "crest_src": p["annamayya_bund_top_level_m"]["source"],
        "surge_peak_m3s": float(p["pincha_surge_peak_m3s"]["value"]),
        "surge_src": p["pincha_surge_peak_m3s"]["source"],
        "travel_min": float(p["pincha_surge_travel_time_min"]["value"]),
        "pincha_mcm": float(p["pincha_storage_mcm"]["value"]),
        "pincha_surcharge_mcm": float(p["pincha_storage_mcm"]["surcharge_max_mcm"]),
        "t_pincha_s": float(events["pincha_failure"]["t_s"]),
        "t_overtop_s": float(events["overtopping_initiation"]["t_s"]),
        "t_washout_s": float(events["washout"]["t_s"]),
        # The counterweight. Four gates operating, gate 4 jammed -- this is what
        # the flood had to exceed for the level to climb at all, and it is why
        # the reservoir overtopped instead of passing the flood.
        "spillway_m3s": float(p["spillway_operating_discharge_m3s"]["value"]),
        "spillway_src": p["spillway_operating_discharge_m3s"]["source"],
        "jammed_gates": p["spillway_operating_discharge_m3s"].get("jammed_gates"),
    }


def catchment_runoff_m3s(t_s: np.ndarray) -> np.ndarray:
    """The catchment's own inflow -- the dominant term, not Pincha.

    Same sourced Gaussian `scripts/route_annamayya.py::catchment_runoff` uses:
    `SCENARIOS['annamayya']['cascade']['catchment_runoff']`, base 800 m3/s,
    peak 3,800 m3/s, offset -1,800 s, sigma 5,400 s, derived from EVD-01's
    180 mm rainfall. Read from `SCENARIOS` directly rather than imported, so
    this module does not drag in route_annamayya's heavy import graph; the
    config is the single source either way.
    """
    import sys
    sys.path.insert(0, str(ROOT))
    from src.data_fetcher import SCENARIOS
    cfg = SCENARIOS["annamayya"]["cascade"]["catchment_runoff"]
    base, peak = float(cfg["base_m3s"]), float(cfg["peak_m3s"])
    off, sig = float(cfg["peak_offset_s"]), float(cfg["sigma_s"])
    return base + (peak - base) * np.exp(-0.5 * ((t_s - off) / sig) ** 2)


def _side_mask(shape, transform, breach_xy, axis_dir, positive: bool):
    """Half-plane on one side of the dam axis."""
    ny, nx = shape
    rows, cols = np.mgrid[0:ny, 0:nx]
    xs, ys = rasterio.transform.xy(transform, rows.ravel(), cols.ravel())
    xs = np.asarray(xs).reshape(ny, nx)
    ys = np.asarray(ys).reshape(ny, nx)
    a = np.asarray(axis_dir, dtype=float)
    a = a / (np.linalg.norm(a) or 1.0)
    normal = np.array([-a[1], a[0]])
    side = (xs - breach_xy[0]) * normal[0] + (ys - breach_xy[1]) * normal[1]
    return side >= 0.0 if positive else side < 0.0


def _upstream_side(dem, transform, breach_xy, axis_dir, radius_m: float = 4000.0):
    """Which side of the dam axis is the reservoir.

    Picked from the terrain, not from vertex order, so a re-authored centerline
    cannot silently put the pool in the gorge below the dam.

    The discriminator is the DSM's own flat water plane: cells within 0.15 m of
    192.50 m, counted near the axis. A reservoir surface in a DSM is flat to a
    few centimetres over square kilometres, and nothing else in this terrain is.
    Measured on annamayya: 2,626 such cells on the reservoir side against 119 on
    the gorge side -- a 22x margin.

    Mean elevation was tried first and is WRONG here: the gorge side reads
    284.38 m against the reservoir side's 273.14 m, because both halves are
    mostly hillside and the reservoir is the low ground. It picked the gorge,
    and the FRL->crest storage came out as 2,046 MCM against a true 15.96.
    """
    ny, nx = dem.shape
    rows, cols = np.mgrid[0:ny, 0:nx]
    xs, ys = rasterio.transform.xy(transform, rows.ravel(), cols.ravel())
    xs = np.asarray(xs).reshape(ny, nx)
    ys = np.asarray(ys).reshape(ny, nx)
    near = ((xs - breach_xy[0]) ** 2 + (ys - breach_xy[1]) ** 2) <= radius_m ** 2
    flat = np.isfinite(dem) & (np.abs(dem - DSM_POOL_PLANE_M) <= 0.15)
    pos = _side_mask(dem.shape, transform, breach_xy, axis_dir, True)
    n_pos = int((flat & near & pos).sum())
    n_neg = int((flat & near & ~pos).sum())
    if max(n_pos, n_neg) == 0:
        raise RuntimeError("no flat water plane near the dam axis on either side")
    return pos if n_pos >= n_neg else ~pos


def reservoir_mask(dem: np.ndarray, transform, breach_xy: tuple[float, float],
                   axis_dir: np.ndarray) -> tuple[np.ndarray, tuple[int, int]]:
    """The DSM water plane upstream of the dam axis, and a seed cell in it.

    The dam is absent from the DEM, so there is nothing to hold water back and
    a naive fill runs straight down the Cheyyeru gorge. Rather than inventing a
    barrier, the pool is clipped by the half-plane on the UPSTREAM side of the
    sourced dam axis. That is a geometric clip, stated as one -- it makes no
    claim about a structure.
    """
    plane = np.isfinite(dem) & (dem <= DSM_POOL_PLANE_M + _PLANE_TOL_M)
    # Clip to the upstream side BEFORE labelling. The dam is not in the DEM and
    # the Cheyyeru falls away downstream to 0.3 m, so every cell from here to
    # the coast is also "below the pool plane" -- label first and the reservoir
    # merges with the whole floodplain. Measured at dx=152 m: that mistake gave
    # a FRL->crest storage of 2,038 MCM against a true 15.96 MCM.
    plane = plane & _upstream_side(dem, transform, breach_xy, axis_dir)
    labels, _ = ndimage.label(plane, structure=np.ones((3, 3), dtype=bool))
    brow, bcol = (int(v) for v in rasterio.transform.rowcol(transform, *breach_xy))

    # Nearest labelled component to the breach point is the reservoir.
    best, best_d = 0, np.inf
    for lab in range(1, labels.max() + 1):
        rr, cc = np.where(labels == lab)
        d = float(np.min((rr - brow) ** 2 + (cc - bcol) ** 2))
        if d < best_d:
            best, best_d = lab, d
    if best == 0:
        raise RuntimeError("no DSM water plane found near the breach point")
    pool = labels == best
    rr, cc = np.where(pool)
    seed = (int(rr[len(rr) // 2]), int(cc[len(cc) // 2]))
    return pool, seed


def upstream_halfplane(dem, transform, breach_xy, axis_dir):
    """Cells on the reservoir side of the dam axis.

    Thin wrapper over `_upstream_side`, which picks the side from the terrain
    rather than from vertex order. Kept as a named function because the clip is
    a modelling statement -- it is a geometric boundary, NOT a barrier, and it
    holds no water back in any solver.
    """
    return _upstream_side(dem, transform, breach_xy, axis_dir)


def hypsometry(dem, transform, pool, upstream, z_levels):
    """Area and volume above the DSM pool plane, per stage level.

    Volume is `sum(z - max(dem, plane))` over cells that are below `z`,
    connected to the pool, and upstream of the axis. The `max(dem, plane)` floor
    is what keeps this honest: no bathymetry exists under the observed water
    surface, so nothing is integrated below it.
    """
    pix = abs(transform.a * transform.e)
    floor = np.maximum(dem, DSM_POOL_PLANE_M)
    areas, volumes = [], []
    for z in z_levels:
        wet = (floor < z) & upstream
        # Keep only what touches the reservoir itself.
        lab, _ = ndimage.label(wet, structure=np.ones((3, 3), dtype=bool))
        keep = np.unique(lab[pool & (lab > 0)])
        wet = np.isin(lab, keep) if keep.size else np.zeros_like(wet)
        areas.append(float(wet.sum()) * pix)
        volumes.append(float(np.sum(np.where(wet, z - floor, 0.0)) * pix))
    return np.asarray(areas), np.asarray(volumes)


def inflow_m3s(t_s: np.ndarray, s: dict, catchment_fn=None) -> np.ndarray:
    """Q(t) into Annamayya: the Pincha surge, lagged by its sourced travel time.

    Shape is a symmetric triangle whose PEAK is the sourced 3,964.4 m3/s and
    whose AREA is the sourced Pincha storage. Both endpoints are sourced, so the
    only assumption is the triangle itself -- stated, and the crossing time it
    produces is reported rather than tuned. A rectangular or gaussian pulse of
    the same peak and volume shifts the crossing by a few minutes; that is
    inside the event clock's own +-1800 s uncertainty on the Pincha time.
    """
    arrival_s = s["t_pincha_s"] + s["travel_min"] * 60.0
    peak = s["surge_peak_m3s"]
    # Triangle of area V and height peak has base 2V/peak.
    base_s = 2.0 * (s["pincha_mcm"] * 1e6) / peak
    half = base_s / 2.0
    q = np.clip(1.0 - np.abs(t_s - (arrival_s + half)) / half, 0.0, None) * peak
    fn = catchment_fn if catchment_fn is not None else catchment_runoff_m3s
    return q + np.asarray(fn(t_s), dtype=float)


def stage_curve(dem, transform, breach_xy, axis_dir, s: dict,
                dt_s: float = 30.0, catchment_fn=None):
    """Integrate dV = Q dt from FRL at the Pincha failure time.

    Returns (t_s, z_m, q_m3s, diagnostics). The level is capped at the crest:
    above it the water leaves over the bund, which is the overtopping this run's
    Stage 2 already represents as a prescribed release.
    """
    z_grid = np.arange(DSM_POOL_PLANE_M, s["crest_m"] + 2.0, 0.05)
    pool, seed = reservoir_mask(dem, transform, breach_xy, axis_dir)
    upstream = upstream_halfplane(dem, transform, breach_xy, axis_dir)
    _, vol_grid = hypsometry(dem, transform, pool, upstream, z_grid)

    t = np.arange(s["t_pincha_s"], s["t_washout_s"] + dt_s, dt_s)
    q_in = inflow_m3s(t, s, catchment_fn)
    # Net of the spillway. The reservoir sits at FRL with its gates open, so
    # only the inflow the spillway cannot pass raises the level -- and with one
    # gate jammed it could not pass this flood. Clipped at zero: a deficit does
    # not draw the pool down here, because below FRL the gates would be closing
    # and that regulation is not in evidence.
    q_net = np.clip(q_in - s["spillway_m3s"], 0.0, None)
    v0 = float(np.interp(s["frl_m"], z_grid, vol_grid))
    v = v0 + np.concatenate([[0.0], np.cumsum(0.5 * (q_net[1:] + q_net[:-1]) * dt_s)])
    z = np.interp(v, vol_grid, z_grid)
    z = np.minimum(z, s["crest_m"])
    q = q_in

    crossed = np.where(z >= s["crest_m"] - 1e-6)[0]
    t_cross = float(t[crossed[0]]) if crossed.size else None
    diag = {
        "v_at_frl_m3": v0,
        "v_at_crest_m3": float(np.interp(s["crest_m"], z_grid, vol_grid)),
        "storage_frl_to_crest_m3": float(np.interp(s["crest_m"], z_grid, vol_grid) - v0),
        "inflow_volume_m3": float(np.trapezoid(q_in, t)),
        "net_volume_m3": float(np.trapezoid(q_net, t)),
        "spillway_m3s": s["spillway_m3s"],
        "t_crest_reached_s": t_cross,
        "t_crest_sourced_s": s["t_overtop_s"],
        "crest_timing_gap_s": (None if t_cross is None
                               else round(t_cross - s["t_overtop_s"], 1)),
        "pool_cells": int(pool.sum()),
        "reached_crest": bool(crossed.size),
    }
    return t, z, q, diag


def stage_curve_on_clock(dem, transform, breach_xy, axis_dir, s: dict,
                         n_frames: int = 48, dt_s: float = 30.0):
    """The Stage-1 curve the demo plays: sourced CLOCK, mass-balance SHAPE.

    Two things are known and neither is negotiable:

      * the reservoir was at FRL 203.6 m and overtopped its 206.0 m bund
        (both OBSERVED), and
      * overtopping began at T-2700 s, washout at T=0 (the event clock).

    So the endpoints and their times are sourced, and this function pins them.
    What the sourced figures do NOT determine is the shape between, and a
    straight ramp would be an invention of a different kind -- so the shape is
    taken from `stage_curve`'s mass balance, normalised onto the clock.

    The mass-balance run is kept and reported, not discarded: it says the
    sourced inflow net of the sourced spillway delivers only PART of the
    storage the rise requires. That shortfall is a finding about the published
    figures and it travels with every frame in `mass_balance_note`. It is not
    closed by adjusting an inflow.
    """
    t_raw, z_raw, q_raw, diag = stage_curve(dem, transform, breach_xy, axis_dir,
                                            s, dt_s=dt_s)
    # Normalised rise shape from the physics, 0 -> 1.
    span = float(z_raw[-1] - z_raw[0])
    shape_u = ((z_raw - z_raw[0]) / span) if span > 1e-9 else np.linspace(0, 1, len(z_raw))

    t_start, t_overtop, t_end = s["t_pincha_s"], s["t_overtop_s"], s["t_washout_s"]
    t = np.linspace(t_start, t_end, n_frames)
    # Rise occupies T_pincha -> T_overtop; the crest is then held to washout.
    frac = np.clip((t - t_start) / (t_overtop - t_start), 0.0, 1.0)
    u = np.interp(frac, np.linspace(0.0, 1.0, len(shape_u)), shape_u)
    z = s["frl_m"] + u * (s["crest_m"] - s["frl_m"])
    q = np.interp(t, t_raw, q_raw)

    required = diag["storage_frl_to_crest_m3"]
    delivered = max(0.0, diag["net_volume_m3"])
    diag["required_storage_m3"] = required
    diag["sourced_fraction_of_required"] = (delivered / required) if required > 0 else None
    diag["mass_balance_note"] = (
        f"Level is pinned to the SOURCED event clock (FRL {s['frl_m']} m at "
        f"T{t_start/60:+.0f} min, crest {s['crest_m']} m at T{t_overtop/60:+.0f} min, "
        f"both OBSERVED); the shape between is the mass balance. Checked, not "
        f"tuned: the sourced inflow net of the sourced {s['spillway_m3s']:.0f} m3/s "
        f"spillway delivers {delivered/1e6:.2f} MCM against the {required/1e6:.2f} MCM "
        f"this rise requires ({100*delivered/required:.0f}%), reaching only "
        f"{z_raw.max():.2f} m unaided. The published figures do not fully account "
        f"for the documented overtopping; the gap is reported, not closed.")
    return t, z, q, diag


def write_frames(dem, transform, crs, breach_xy, axis_dir, s: dict, out_dir: Path,
                 n_frames: int = 48, start_idx: int = 0) -> list[dict]:
    """Emit reservoir_rise frames into `out_dir`, returning index entries.

    Frame schema, stage label and phase-title wording all match what
    `run_pipeline.py`'s cascade branch already emits, so the existing frontend
    (`frontend/map.js`, which branches on `stage === "reservoir_rise"`) renders
    these with no change.
    """
    import sys
    sys.path.insert(0, str(ROOT))
    import run_pipeline as _rp
    from pyproj import Transformer
    from src.m2_geometry.fill import compute_lake_depth_grids

    t, z, q, diag = stage_curve_on_clock(dem, transform, breach_xy, axis_dir,
                                         s, n_frames=n_frames)
    pool, seed_rc = reservoir_mask(dem, transform, breach_xy, axis_dir)
    upstream = upstream_halfplane(dem, transform, breach_xy, axis_dir)
    to_wgs84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform

    # The pool is clipped to the upstream half-plane by raising everything
    # downstream of the dam axis above any level reached here. That is the same
    # geometric clip `reservoir_mask` documents, applied to the grid the depth
    # frames are cut from -- not a barrier, and it holds no water back in any
    # solver.
    floor = np.maximum(np.nan_to_num(dem, nan=1e6), DSM_POOL_PLANE_M)
    clipped = np.where(upstream, floor, 1e6)
    seed_xy = rasterio.transform.xy(transform, *seed_rc)

    grids = compute_lake_depth_grids(
        dem_array=clipped, transform=transform, seed_xy=seed_xy,
        wse_m=float(s["crest_m"]), levels_m=[float(v) for v in z])

    snaps = out_dir / "snapshots"; snaps.mkdir(parents=True, exist_ok=True)
    prev = out_dir / "preview_rasters"; prev.mkdir(parents=True, exist_ok=True)
    entries = []
    for k, (tt, zz, qq, g) in enumerate(zip(t, z, q, grids)):
        fp = snaps / f"frame_pre_{k:03d}.geojson"
        pp = prev / f"frame_pre_{k:03d}.png"
        fp.write_text(json.dumps(_rp._depth_to_geojson(g["depth_grid"], transform,
                                                       float(tt), to_wgs84)),
                      encoding="utf-8")
        bounds = _rp._write_depth_preview(g["depth_grid"], pp, transform, to_wgs84)
        if zz < s["crest_m"] - 2.0:
            title = f"Reservoir Rising — Pincha surge en route ({zz:.1f} m)"
        elif zz < s["crest_m"] - 1e-6:
            title = f"Approaching Crest ({zz:.2f} m / {s['crest_m']:.1f} m)"
        else:
            title = f"Overtopping Threshold ({zz:.2f} m) — bund is being overtopped"
        entries.append({
            "frame_idx": start_idx + k,
            "t_s": float(tt), "t_min": round(float(tt) / 60.0, 1),
            "stage": "reservoir_rise",
            "phase_title": title,
            "level_m": round(float(zz), 2),
            "inflow_m3s": round(float(qq), 1),
            "volume_mcm": round(g["volume_m3"] / 1e6, 2),
            "area_km2": round(g["area_m2"] / 1e6, 2),
            "level_source": "SOURCED_CLOCK_MASS_BALANCE_SHAPE",
            "path": str(fp), "preview_path": str(pp), "preview_bounds": bounds,
            "provenance": "PRESCRIBED_STAGE_ABOVE_OBSERVED_WATER_SURFACE",
        })
    (out_dir / "stage1_prebreach.json").write_text(
        json.dumps({"diagnostics": diag, "sources": s, "frames": entries},
                   indent=2, default=str), encoding="utf-8")
    return entries


def _demo() -> None:
    """Self-check against the real DEM. Prints the curve and its timing gap."""
    import sys
    sys.path.insert(0, str(ROOT))
    from shapely.geometry import LineString
    from shapely.ops import transform as shp_transform
    from pyproj import Transformer

    s = sourced()
    ev = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    centerline = ev["parameters"]["breach_centerline_wgs84"]

    with rasterio.open(ROOT / "data" / "dem" / "annamayya_dem.tif") as src:
        dem = src.read(1).astype(float)
        tr, crs = src.transform, src.crs
        dem[dem == src.nodata] = np.nan

    to_dem = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    axis = shp_transform(to_dem, LineString(centerline))
    a, b = np.array(axis.coords[0]), np.array(axis.coords[-1])
    breach_xy = tuple((a + b) / 2.0)

    t, z, q, diag = stage_curve(dem, tr, breach_xy, b - a, s)

    print(f"sourced FRL {s['frl_m']} m -> crest {s['crest_m']} m")
    print(f"pool cells {diag['pool_cells']}, "
          f"storage FRL->crest {diag['storage_frl_to_crest_m3']/1e6:.3f} MCM "
          "(above the 192.50 m DSM water plane)")
    print(f"sourced inflow volume {diag['inflow_volume_m3']/1e6:.3f} MCM "
          f"(Pincha {s['pincha_mcm']} MCM, peak {s['surge_peak_m3s']} m3/s)")
    for tt in (-10800, -9000, -7200, -5400, -3600, -2700, -1800, -900, 0):
        i = int(np.argmin(np.abs(t - tt)))
        print(f"  T{tt/60:+7.0f} min   z = {z[i]:7.2f} m   Q = {q[i]:8.1f} m3/s")
    if diag["reached_crest"]:
        print(f"crest reached at T{diag['t_crest_reached_s']/60:+.0f} min "
              f"vs sourced T{s['t_overtop_s']/60:+.0f} min "
              f"-> gap {diag['crest_timing_gap_s']/60:+.1f} min")
    else:
        print(f"crest NOT reached by T+0. Max level {z.max():.2f} m "
              f"against crest {s['crest_m']} m. REPORTED, NOT TUNED.")
    assert z[0] <= s["frl_m"] + 1e-6, "curve must start at or below FRL"
    assert np.all(np.diff(z) >= -1e-9), "level must be monotone non-decreasing"
    assert z.max() <= s["crest_m"] + 1e-9, "level must be capped at the crest"
    print("self-check OK")


if __name__ == "__main__":
    _demo()
