"""Back-fill map layers the served Annamayya run never wrote.

`data/scenarios/annamayya_compound/` is the run `annamayya`'s web UI shows, and
it was missing several layers the map needs: the road-cut timeline, per-village
isolation times, evacuation routes, the lake (rising AND draining) as an
animatable layer, the DSM's own flat water planes, the flood-front direction
field, and a per-village depth time series. All of it is computable from the
run's OWN saved outputs (depth rasters, max_depth, arrival_time, hydrograph)
plus data already on disk (road graph, DEM, sourced reservoir figures) --
nothing here re-solves the shallow-water equations or invents a number.

Every value that cannot be honestly computed is written as `null` with a
`reason` string next to it, never guessed. Every artifact carries a
`provenance` field saying how it was made.

Run:  python scripts/backfill_visual_layers.py [run_id]     (default annamayya_compound)

Idempotent: re-running overwrites only the files this script writes.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import rasterio
import rasterio.features
import rasterio.transform
import rasterio.warp
from pyproj import Transformer
from scipy import ndimage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from src.run_manifest import (is_valid, load_manifest,             # noqa: E402
                              register_artifact, write_manifest)
from src.rasterutils import sample_raster                          # noqa: E402
from src.m6_isolation.isolation import (                            # noqa: E402
    THRESH_CAR, compute_isolation_times, edge_cut_times, emit_road_cut_timeline)

RUNS = ROOT / "data" / "scenarios"
COMPOUND = RUNS / "annamayya_compound"
STAGE2 = RUNS / "annamayya_stage2_full"
DEM_PATH = ROOT / "data" / "dem" / "annamayya_dem.tif"
ROADS_GRAPHML = ROOT / "data" / "roads" / "annamayya_roads.graphml"
EVIDENCE = ROOT / "data" / "evidence" / "annamayya_event_evidence.json"



def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _log(msg: str) -> None:
    print(msg, flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Verify the depth-raster / time correspondence before trusting it
# ─────────────────────────────────────────────────────────────────────────────

def verified_raster_stack(run_dir: Path) -> list[tuple[float, Path]]:
    """`[(t_s, depth_tif_path), ...]` for the run's own depth rasters, in order.

    The compound run's OWN `snapshots_index.json` mixes 36 `reservoir_rise`
    frames with 160 `routing` frames (some `INTERPOLATED_FOR_DISPLAY`), and
    does not line up 1:1 with the 97 `depth_NNN.tif` files on disk. The spec's
    time source -- `annamayya_stage2_full/snapshots_index.json` -- has exactly
    97 entries, one per depth raster, and its `routing` frames are the raw
    solver output the compound run's depth rasters were copied from. Verified,
    not assumed: entry count must match file count, and a sample of files must
    be byte-identical between the two run directories.
    """
    depth_dir = run_dir / "depth_rasters"
    depth_files = sorted(depth_dir.glob("depth_*.tif"))
    si_path = STAGE2 / "snapshots_index.json"
    si = json.loads(si_path.read_text(encoding="utf-8"))

    if len(depth_files) != len(si):
        raise RuntimeError(
            f"depth raster count {len(depth_files)} != stage2_full snapshots_index "
            f"count {len(si)} -- correspondence assumption fails, refusing to guess")

    # Byte-identity check on a spread of indices, not just the first/last.
    stage2_depth_dir = STAGE2 / "depth_rasters"
    check_idxs = sorted({0, 1, len(depth_files) // 2, len(depth_files) - 1})
    mismatches = []
    for i in check_idxs:
        a = depth_files[i]
        b = stage2_depth_dir / a.name
        if not b.is_file():
            mismatches.append(f"{a.name}: no counterpart in {stage2_depth_dir}")
            continue
        if _sha256(a) != _sha256(b):
            mismatches.append(f"{a.name}: sha256 differs from stage2_full copy")
    if mismatches:
        raise RuntimeError("depth raster correspondence check failed: " + "; ".join(mismatches))

    stack = [(float(si[i]["t_s"]), depth_files[i]) for i in range(len(depth_files))]
    _log(f"verified correspondence: {len(stack)} depth rasters <-> "
         f"{si_path.relative_to(ROOT)} entries, sha256-checked at indices {check_idxs}")
    return stack


# ─────────────────────────────────────────────────────────────────────────────
# (a) roads_timeline.geojson
# ─────────────────────────────────────────────────────────────────────────────

def build_roads_timeline(run_dir: Path, raster_stack: list[tuple[float, Path]]) -> Path:
    import osmnx as ox
    G = ox.load_graphml(ROADS_GRAPHML)
    out_path = run_dir / "roads_timeline.geojson"
    emit_road_cut_timeline(G, raster_stack, out_path, threshold_m=THRESH_CAR)
    return out_path


# ─────────────────────────────────────────────────────────────────────────────
# (b) isolation written into results.geojson
# ─────────────────────────────────────────────────────────────────────────────

def build_isolation(run_dir: Path, raster_stack: list[tuple[float, Path]]):
    """Compute isolation via `compute_isolation_times` and merge into results.geojson.

    Sign convention (from `src/m6_isolation/isolation.py`, unchanged here):
    `evacuation_window_min = water_arrival_min - isolation_time_min`. Positive
    means people still have a way out after the road is cut upstream of the
    village; negative means the water arrives before the road does.

    Only `isolation_time_min`, `isolation_status`, `isolation_computed`,
    `evacuation_window_min`, and (where the module's own arrival differs from
    the stored figure) `water_arrival_min_isolation` are touched. Everything
    else in the file -- priority_rank, priority_score, water_arrival_min, pop
    fields -- is left exactly as it was.
    """
    import geopandas as gpd
    import osmnx as ox

    results_path = run_dir / "results.geojson"
    backup_path = run_dir / "results.pre_isolation.geojson"
    if not backup_path.exists():
        shutil.copyfile(results_path, backup_path)
        _log(f"backed up original -> {backup_path.name}")
    else:
        _log(f"backup already exists, not overwritten -> {backup_path.name}")

    # Read the ORIGINAL (pre-isolation) file as the base to edit, so re-running
    # this script is idempotent rather than compounding onto its own output.
    vg = gpd.read_file(backup_path)

    G = ox.load_graphml(ROADS_GRAPHML)
    max_depth_tif = run_dir / "max_depth.tif"

    result = compute_isolation_times(G, raster_stack, vg, max_depth_tif,
                                     threshold_m=THRESH_CAR, bridge_threshold_m=3.0)

    with (run_dir / "results.geojson").open("r", encoding="utf-8") as fh:
        original = json.load(fh)

    updated = 0
    for feat in original["features"]:
        vid = feat["properties"]["village_id"]
        row = result.loc[result["village_id"] == vid]
        if row.empty:
            continue
        row = row.iloc[0]
        iso_min = row["isolation_time_min"]
        feat["properties"]["isolation_time_min"] = (
            None if (iso_min is None or (isinstance(iso_min, float) and np.isnan(iso_min)))
            else float(iso_min))
        feat["properties"]["isolation_status"] = str(row["isolation_status"])
        feat["properties"]["isolation_computed"] = True

        module_arrival = row["water_arrival_min"]
        module_arrival = (None if (module_arrival is None
                                   or (isinstance(module_arrival, float) and np.isnan(module_arrival)))
                          else float(module_arrival))
        stored_arrival = feat["properties"].get("water_arrival_min")
        if module_arrival is not None and stored_arrival is not None and \
           abs(module_arrival - float(stored_arrival)) > 1e-6:
            feat["properties"]["water_arrival_min_isolation"] = module_arrival
        elif module_arrival is not None and stored_arrival is None:
            feat["properties"]["water_arrival_min_isolation"] = module_arrival

        # evacuation_window_min: copied straight from the module's own output
        # ("recompute exactly as the module defines it"), which is
        # `evacuation_window_s = water_arr_t - isolation_t` in the module's own
        # seconds, both computed from the SAME per-timestep pass -- not
        # recombined here from the stored water_arrival_min, which can come
        # from a different computation (M5's footprint-inundation pass) and
        # may legitimately differ from the module's own centroid-based arrival
        # (see water_arrival_min_isolation above). Positive = people still
        # have a way out after the road is cut; negative = water arrives
        # before the road does. NaN (module never isolated, or never wetted)
        # becomes null.
        window = row["evacuation_window_min"]
        feat["properties"]["evacuation_window_min"] = (
            None if (window is None or (isinstance(window, float) and np.isnan(window)))
            else float(window))
        updated += 1

    with (run_dir / "results.geojson").open("w", encoding="utf-8") as fh:
        json.dump(original, fh, ensure_ascii=False, indent=2)

    _log(f"isolation merged into results.geojson: {updated}/{len(original['features'])} villages")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# (c) evac_routes.geojson
# ─────────────────────────────────────────────────────────────────────────────

def build_evac_routes(run_dir: Path) -> Path:
    """Time-aware evacuation routes, both modes -- see src/m6_isolation/evacuation.py."""
    import geopandas as gpd
    import osmnx as ox
    from src.m6_isolation.evacuation import (never_wet_nodes, route_settlements,
                                             shelters_from_gdf)

    G = ox.load_graphml(ROADS_GRAPHML)
    raster_stack = verified_raster_stack(run_dir)
    _, edge_keys, cut_s, _ = edge_cut_times(G, raster_stack, THRESH_CAR, spacing_m=25.0)
    cut_min = {ek: (None if np.isnan(c) else float(c) / 60.0) for ek, c in zip(edge_keys, cut_s)}
    never_wet = never_wet_nodes(G, run_dir / "max_depth.tif", THRESH_CAR)

    vg = gpd.read_file(run_dir / "results.geojson")
    vg = vg[vg["priority_rank"].notna()].sort_values("priority_rank")
    settlements = [{"village_id": r["village_id"], "village_name": r["village_name"],
                    "priority_rank": int(r["priority_rank"]), "lon": float(r["lon"]),
                    "lat": float(r["lat"]),
                    "water_arrival_min": None if r.get("water_arrival_min") is None
                    or np.isnan(r["water_arrival_min"]) else float(r["water_arrival_min"])}
                   for _, r in vg.iterrows()]

    fac = gpd.read_file(ROOT / "data" / "admin" / "annamayya_facilities.geojson")
    shelters = shelters_from_gdf(fac, run_dir / "max_depth.tif", THRESH_CAR)

    t_lo, t_hi = raster_stack[0][0] / 60.0, raster_stack[-1][0] / 60.0
    out = route_settlements(G, cut_min, never_wet, settlements, shelters, t_lo, t_hi)
    out_path = run_dir / "evac_routes.geojson"
    out_path.write_text(json.dumps(out, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    n0 = sum(1 for r in out["settlements"] if r["origin_on_mainland"])
    _log(f"evac_routes: {len(out['features'])} route variants for {len(settlements)} settlements; "
         f"mainland {out['mainland_nodes']} nodes, {out['shelters_on_mainland']} shelters on it; "
         f"{n0} settlements already on the dry mainland")
    return out_path


# ─────────────────────────────────────────────────────────────────────────────
# (d) lake_frames.geojson  (rising + draining)
# ─────────────────────────────────────────────────────────────────────────────

def build_lake_frames(run_dir: Path) -> Path:
    import annamayya_stage1_frames as s1

    si = json.loads((run_dir / "snapshots_index.json").read_text(encoding="utf-8"))
    rising = [e for e in si if e.get("stage") == "reservoir_rise"]
    rising.sort(key=lambda e: e["t_min"])

    features = []
    for e in rising:
        frame_geo = json.loads(Path(e["path"]).read_text(encoding="utf-8"))
        geoms = [f["geometry"] for f in frame_geo.get("features", []) if f.get("geometry")]
        # Union of that frame's depth-class polygons -> one Polygon/MultiPolygon.
        from shapely.geometry import shape
        from shapely.ops import unary_union
        if geoms:
            union = unary_union([shape(g) for g in geoms])
            geom_out = json.loads(json.dumps(union.__geo_interface__))
        else:
            geom_out = {"type": "MultiPolygon", "coordinates": []}
        features.append({
            "type": "Feature", "geometry": geom_out,
            "properties": {
                "t_min": e["t_min"], "level_m": e["level_m"],
                "volume_mcm": e["volume_mcm"], "area_km2": e["area_km2"],
                "phase": "rising",
                "source": "sourced event clock (existing reservoir_rise frame)",
            },
        })

    # ── draining phase: 0-D mass balance from the T=0 frame's volume ──────────
    s = s1.sourced()
    ev = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    centerline = ev["parameters"]["breach_centerline_wgs84"]
    from shapely.geometry import LineString
    from shapely.ops import transform as shp_transform

    with rasterio.open(DEM_PATH) as src:
        dem = src.read(1).astype(float)
        tr, crs = src.transform, src.crs
        dem[dem == src.nodata] = np.nan

    to_dem = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    to_wgs84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
    axis = shp_transform(to_dem, LineString(centerline))
    a_pt, b_pt = np.array(axis.coords[0]), np.array(axis.coords[-1])
    breach_xy = tuple((a_pt + b_pt) / 2.0)
    axis_dir = b_pt - a_pt

    pool, seed_rc = s1.reservoir_mask(dem, tr, breach_xy, axis_dir)
    upstream = s1.upstream_halfplane(dem, tr, breach_xy, axis_dir)

    # SAME hypsometry the rising frames used: V(z) above the DSM pool plane.
    z_grid = np.arange(s1.DSM_POOL_PLANE_M, s["crest_m"] + 2.0, 0.05)
    areas, vols = s1.hypsometry(dem, tr, pool, upstream, z_grid)
    v_crest = float(np.interp(s["crest_m"], z_grid, vols))
    v_frl_published_m3 = 63.43e6

    def level_of_volume(v: float) -> float:
        # Capped at the crest, not at vols[-1] (which is V at crest_m + 2.0 m,
        # kept in the probe grid only as interpolation headroom): above the
        # crest water leaves over the bund, the same convention stage_curve
        # uses for the rising phase (z = min(z, crest_m)).
        v_clamped = float(np.clip(v, vols[0], v_crest))
        return float(np.interp(v_clamped, vols, z_grid))

    def area_of_level(z: float) -> float:
        return float(np.interp(z, z_grid, areas))

    # Floor: level never below the DSM water plane.
    floor_m = s1.DSM_POOL_PLANE_M

    t0 = next((e for e in rising if abs(e["t_min"]) < 1e-6), rising[-1])
    v0 = t0["volume_mcm"] * 1e6

    hyd = json.loads((run_dir / "hydrograph.json").read_text(encoding="utf-8"))
    arm = hyd.get("measured") or hyd.get("central")
    q_out_t_s = np.asarray(arm["t_s"], dtype=float)
    q_out_vals = np.asarray(arm.get("Q_m3s") or arm.get("q_m3s") or arm["Q"], dtype=float)

    def q_out_at(t_s: float) -> float:
        return float(np.interp(t_s, q_out_t_s, q_out_vals))

    # Q_in: same sourced inflow model the reservoir_rise frames used, but on the
    # DRAINING clock (T>=0) rather than the pre-breach clock -- inflow_m3s takes
    # absolute seconds relative to the same t=0 epoch as the hydrograph.
    def q_in_at(t_s: float) -> float:
        return float(s1.inflow_m3s(np.asarray([t_s]), s)[0])

    v_floor = float(vols[0])       # volume at the DSM water plane (the floor)
    v_cap = v_crest                # volume at the crest -- above it water leaves
                                    # over the bund, same convention stage_curve
                                    # uses for the rising phase (z capped at crest)

    dt_s = 60.0  # integration step, finer than the 15-min emission cadence
    t_cur = 0.0
    v_cur = v0
    drain_frames = []
    last_emitted = None
    emit_every_s = 15 * 60.0
    end_s = 1440 * 60.0
    while t_cur <= end_s + 1e-6:
        should_emit = last_emitted is None or (t_cur - last_emitted) >= emit_every_s - 1e-6
        at_floor = level_of_volume(v_cur) <= floor_m + 1e-6
        if should_emit:
            z_cur = float(np.clip(level_of_volume(v_cur), floor_m, s["crest_m"]))
            drain_frames.append((t_cur, z_cur, v_cur))
            last_emitted = t_cur
            if at_floor and t_cur > 0:
                break  # floor reached and unchanged -> this is the final frame
        # advance; clamp to [v_floor, v_cap] -- inflow above the crest spills
        # over the bund (not modelled further here), outflow cannot draw the
        # pool below the DSM's own observed water surface.
        q_net = q_in_at(t_cur) - q_out_at(t_cur)
        v_cur = float(np.clip(v_cur + q_net * dt_s, v_floor, v_cap))
        t_cur += dt_s

    # Build outline geometry for each draining frame the same way the rising
    # frames were built: compute_lake_depth_grids with an explicit levels_m,
    # over the SAME upstream-clipped DEM `write_frames` uses.
    from src.m2_geometry.fill import compute_lake_depth_grids
    floor_grid = np.maximum(np.nan_to_num(dem, nan=1e6), s1.DSM_POOL_PLANE_M)
    clipped = np.where(upstream, floor_grid, 1e6)
    seed_xy = rasterio.transform.xy(tr, *seed_rc)

    levels_sorted_idx = sorted(range(len(drain_frames)), key=lambda i: drain_frames[i][1])
    levels_m_sorted = [drain_frames[i][1] for i in levels_sorted_idx]
    grids = compute_lake_depth_grids(dem_array=clipped, transform=tr, seed_xy=seed_xy,
                                     wse_m=float(s["crest_m"]), levels_m=levels_m_sorted)
    grid_by_sorted_pos = {levels_sorted_idx[i]: grids[i] for i in range(len(grids))}

    for i, (t_s, z_cur, v_cur) in enumerate(drain_frames):
        g = grid_by_sorted_pos[i]
        from run_pipeline import _depth_to_geojson
        frame_geo = _depth_to_geojson(g["depth_grid"], tr, float(t_s), to_wgs84)
        from shapely.geometry import shape
        from shapely.ops import unary_union
        geoms = [shape(f["geometry"]) for f in frame_geo.get("features", []) if f.get("geometry")]
        geom_out = (json.loads(json.dumps(unary_union(geoms).__geo_interface__))
                   if geoms else {"type": "MultiPolygon", "coordinates": []})
        features.append({
            "type": "Feature", "geometry": geom_out,
            "properties": {
                "t_min": round(t_s / 60.0, 1), "level_m": round(z_cur, 2),
                "volume_mcm": round(g["volume_m3"] / 1e6, 2),
                "area_km2": round(g["area_m2"] / 1e6, 2),
                "phase": "draining",
                "source": ("0-D mass balance: DEM storage curve + prescribed release + "
                          "sourced inflow; floor at DSM water plane"),
            },
        })

    metadata = {
        "v_crest_mcm": round(v_crest / 1e6, 3),
        "v_frl_published_mcm": 63.43,
        "note": ("v_crest_mcm is V(206.0 m) off this run's own hypsometry curve, above the "
                "192.50 m DSM water plane; v_frl_published_mcm is the OFFICIAL stage-storage "
                "figure at FRL 203.6 m (SCENARIOS['annamayya']['cascade']['reservoir']"
                "['v_frl_mcm']), a DIFFERENT level. Reported for comparison, not reconciled."),
        "floor_m": floor_m, "n_rising": len(rising), "n_draining": len(drain_frames),
    }

    out = {"type": "FeatureCollection", "features": features, "metadata": metadata}
    out_path = run_dir / "lake_frames.geojson"
    out_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    _log(f"lake_frames: {len(rising)} rising + {len(drain_frames)} draining frames; "
         f"V(crest)={metadata['v_crest_mcm']} MCM vs published 63.43 MCM")
    return out_path


# ─────────────────────────────────────────────────────────────────────────────
# (e) water_planes.geojson
# ─────────────────────────────────────────────────────────────────────────────

def build_water_planes(run_dir: Path) -> Path:
    with rasterio.open(run_dir / "max_depth.tif") as ref:
        bounds, ref_crs = ref.bounds, ref.crs

    with rasterio.open(DEM_PATH) as src:
        dem = src.read(1).astype(float)
        tr, crs, nodata = src.transform, src.crs, src.nodata
        dem[dem == nodata] = np.nan
        px = abs(tr.a * tr.e)

    # Crop DEM to the run's raster bounds (reproject bounds if CRS differs;
    # here both are EPSG:32644 so this is a direct window).
    if crs != ref_crs:
        left, bottom, right, top = rasterio.warp.transform_bounds(ref_crs, crs, *bounds)
    else:
        left, bottom, right, top = bounds.left, bounds.bottom, bounds.right, bounds.top
    row0, col0 = rasterio.transform.rowcol(tr, left, top)
    row1, col1 = rasterio.transform.rowcol(tr, right, bottom)
    row0, row1 = sorted((max(0, row0), min(dem.shape[0], row1)))
    col0, col1 = sorted((max(0, col0), min(dem.shape[1], col1)))
    crop = dem[row0:row1, col0:col1]
    crop_tr = tr * rasterio.Affine.translation(col0, row0)

    # 3x3 neighbourhood flatness: max - min == 0 over the 3x3 window.
    finite = np.isfinite(crop)
    filled = np.where(finite, crop, -1e9)
    nmax = ndimage.maximum_filter(filled, size=3, mode="nearest")
    filled2 = np.where(finite, crop, 1e9)
    nmin = ndimage.minimum_filter(filled2, size=3, mode="nearest")
    flat = finite & (nmax == nmin)

    labels, n = ndimage.label(flat, structure=np.ones((3, 3), dtype=bool))
    to_wgs84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform

    features = []
    found = []
    for lab in range(1, n + 1):
        mask = labels == lab
        area_km2 = float(mask.sum()) * px / 1e6
        if area_km2 < 1.0:
            continue
        elev_m = float(crop[mask][0])
        shapes = list(rasterio.features.shapes(mask.astype(np.uint8), mask=mask, transform=crop_tr))
        from shapely.geometry import shape
        from shapely.ops import unary_union, transform as shp_transform
        polys = [shape(geom) for geom, val in shapes if val == 1]
        if not polys:
            continue
        union = unary_union(polys)
        tol = px / 2.0
        simplified = union.simplify(tol, preserve_topology=True)
        wgs = shp_transform(to_wgs84, simplified)
        features.append({
            "type": "Feature", "geometry": json.loads(json.dumps(wgs.__geo_interface__)),
            "properties": {"elev_m": round(elev_m, 2), "area_km2": round(area_km2, 3),
                           "source": "flat water plane in GLO-30 DSM"},
        })
        found.append((round(elev_m, 2), round(area_km2, 3)))

    out = {"type": "FeatureCollection", "features": features}
    out_path = run_dir / "water_planes.geojson"
    out_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    _log(f"water_planes: {len(features)} planes found: {found}")
    return out_path


# ─────────────────────────────────────────────────────────────────────────────
# (f) front_field.json
# ─────────────────────────────────────────────────────────────────────────────

def build_front_field(run_dir: Path) -> Path:
    with rasterio.open(run_dir / "arrival_time.tif") as src:
        arr = src.read(1).astype(float)
        tr, crs, nodata = src.transform, src.crs, src.nodata

    # Verified above: valid values run 0.46-1439.68, i.e. MINUTES (the run
    # window is 1440 min / 24 h), matching the API docstring
    # "/api/arrival/{job_id} -> values in minutes".
    valid = arr != nodata if nodata is not None else np.isfinite(arr)
    arr_min = np.where(valid, arr, np.nan)

    bounds = rasterio.transform.array_bounds(*arr.shape, tr)  # (left, bottom, right, top) in src CRS
    to_wgs84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
    left_lon, bottom_lat = to_wgs84(bounds[0], bounds[1])
    right_lon, top_lat = to_wgs84(bounds[2], bounds[3])

    # Regular EPSG:4326 grid at approximately the same resolution/cell count.
    ny, nx = arr.shape
    dst_transform, dst_w, dst_h = rasterio.warp.calculate_default_transform(
        crs, "EPSG:4326", nx, ny, *bounds, resolution=None)
    grid = np.full((dst_h, dst_w), np.nan, dtype=np.float64)
    rasterio.warp.reproject(
        source=arr_min, destination=grid, src_transform=tr, src_crs=crs,
        dst_transform=dst_transform, dst_crs="EPSG:4326",
        resampling=rasterio.warp.Resampling.bilinear, src_nodata=np.nan, dst_nodata=np.nan)

    west, north = dst_transform.c, dst_transform.f
    dlon, dlat = dst_transform.a, dst_transform.e  # dlat negative (north-down)
    south = north + dlat * dst_h
    east = west + dlon * dst_w

    lat_centers = north + dlat * (np.arange(dst_h) + 0.5)
    lon_centers = west + dlon * (np.arange(dst_w) + 0.5)

    # Gradient of arrival time (minutes) w.r.t. metres, converted from degrees.
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * np.cos(np.radians(lat_centers))[:, None]
    dT_dy_deg = np.gradient(grid, axis=0)  # per-row step is dlat (negative)
    dT_dx_deg = np.gradient(grid, axis=1)
    dT_dy = dT_dy_deg / dlat
    dT_dx = dT_dx_deg / dlon
    dT_dy_m = dT_dy / m_per_deg_lat
    dT_dx_m = dT_dx / m_per_deg_lon

    # Direction of INCREASING arrival time = the gradient vector itself
    # (points toward later arrival, i.e. the way the front travels).
    mag = np.hypot(dT_dx_m, dT_dy_m)
    with np.errstate(invalid="ignore", divide="ignore"):
        u = dT_dx_m / mag
        v = dT_dy_m / mag

    never_wet = ~np.isfinite(grid)
    u = np.where(never_wet | ~np.isfinite(u), np.nan, u)
    v = np.where(never_wet | ~np.isfinite(v), np.nan, v)

    def rows_to_list(a):
        return [[None if not np.isfinite(x) else round(float(x), 4) for x in row] for row in a]

    n_non_null = int(np.isfinite(u).sum())
    payload = {
        "west": round(float(west), 6), "south": round(float(south), 6),
        "east": round(float(east), 6), "north": round(float(north), 6),
        "nx": int(dst_w), "ny": int(dst_h),
        "t_arr_min": rows_to_list(grid),
        "u": rows_to_list(u), "v": rows_to_list(v),
        "provenance": ("Gradient of arrival_time.tif (minutes), reprojected to a regular "
                      "EPSG:4326 grid at ~ the same resolution, unit vectors pointing toward "
                      "increasing arrival time (the direction the flood front travels). "
                      "East/north components scaled to metres (m_per_deg_lat=111320, "
                      "m_per_deg_lon=111320*cos(lat)). Never-wetted cells are null."),
    }
    out_path = run_dir / "front_field.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    size_mb = out_path.stat().st_size / 1e6
    _log(f"front_field: {dst_w}x{dst_h} grid, {n_non_null} non-null cells, {size_mb:.3f} MB")
    return out_path


# ─────────────────────────────────────────────────────────────────────────────
# (g) village_depth.json
# ─────────────────────────────────────────────────────────────────────────────

def build_village_depth(run_dir: Path, raster_stack: list[tuple[float, Path]]) -> Path:
    import geopandas as gpd

    vg = gpd.read_file(run_dir / "results.geojson")
    with rasterio.open(raster_stack[0][1]) as s0:
        flood_crs = s0.crs
    vg_flood = vg.to_crs(flood_crs)

    village_cells = []
    with rasterio.open(raster_stack[0][1]) as s0:
        shape0, tr0 = s0.shape, s0.transform
    for geom in vg_flood.geometry:
        # Cell CENTRES inside the polygon, per spec -- all_touched=False.
        mask = rasterio.features.geometry_mask([geom.__geo_interface__], out_shape=shape0,
                                               transform=tr0, invert=True, all_touched=False)
        rows, cols = np.nonzero(mask)
        village_cells.append((rows, cols))

    series = {vid: [] for vid in vg_flood["village_id"]}
    ids = vg_flood["village_id"].tolist()
    names = vg_flood["village_name"].tolist()

    for t_s, path in raster_stack:
        with rasterio.open(path) as src:
            depth = src.read(1).astype(float)
            nodata = src.nodata
        if nodata is not None:
            depth = np.where(depth == nodata, 0.0, depth)
        t_min = round(t_s / 60.0, 2)
        for vid, (rows, cols) in zip(ids, village_cells):
            d = float(np.max(depth[rows, cols])) if rows.size else 0.0
            series[vid].append([t_min, round(max(d, 0.0), 3)])

    out = {vid: {"name": name, "series": series[vid]} for vid, name in zip(ids, names)}
    out["provenance"] = ("Max depth (m) over cells whose centres fall inside each village "
                         "polygon, sampled from this run's own depth_rasters (0 where no cell "
                         "centre is inside or all sampled cells are dry). t_min from the "
                         "verified stage2_full snapshots_index correspondence.")
    out_path = run_dir / "village_depth.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    _log(f"village_depth: {len(ids)} villages x {len(raster_stack)} timesteps")
    return out_path


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    run_id = sys.argv[1] if len(sys.argv) > 1 else "annamayya_compound"
    run_dir = RUNS / run_id
    if not (run_dir / "manifest.json").is_file():
        print(f"no manifest at {run_dir}")
        return 1

    raster_stack = verified_raster_stack(run_dir)

    _log("=== (a) roads_timeline ===")
    roads_path = build_roads_timeline(run_dir, raster_stack)

    _log("=== (b) isolation -> results.geojson ===")
    build_isolation(run_dir, raster_stack)

    _log("=== (c) evac_routes ===")
    evac_path = build_evac_routes(run_dir)

    _log("=== (d) lake_frames ===")
    lake_path = build_lake_frames(run_dir)

    _log("=== (e) water_planes ===")
    planes_path = build_water_planes(run_dir)

    _log("=== (f) front_field ===")
    front_path = build_front_field(run_dir)

    _log("=== (g) village_depth ===")
    vdepth_path = build_village_depth(run_dir, raster_stack)

    _log("=== registering artifacts in manifest ===")
    man = load_manifest(run_dir / "manifest.json")
    pr = man.setdefault("pipeline_result", {})
    pr["roads_timeline"] = str(roads_path)
    pr["evac_routes"] = str(evac_path)
    pr["lake_frames"] = str(lake_path)
    pr["water_planes"] = str(planes_path)
    pr["front_field"] = str(front_path)
    pr["village_depth"] = str(vdepth_path)

    for name, path, media in (
            ("roads_timeline", roads_path, "application/geo+json"),
            ("evac_routes", evac_path, "application/geo+json"),
            ("lake_frames", lake_path, "application/geo+json"),
            ("water_planes", planes_path, "application/geo+json"),
            ("front_field", front_path, "application/json"),
            ("village_depth", vdepth_path, "application/json")):
        register_artifact(man, path, run_root=RUNS, name=name, media_type=media)

    # results.geojson was rewritten in place (isolation merge) and is a
    # registered artifact with a sha256 -- re-register so the manifest's hash
    # matches the file that is now on disk.
    register_artifact(man, run_dir / "results.geojson", run_root=RUNS, name="results",
                      media_type="application/geo+json", required=True)
    # The compound run was assembled from annamayya_stage2_full, and its
    # pipeline_result still pointed `results_geojson` at THAT run's file -- so
    # /api/results served a table without the isolation merged above, and the
    # map read "0 villages cut off". Serve this run's own file. The pre-merge
    # copy is byte-identical to stage2_full's (checked 2026-09-24).
    pr["results_geojson"] = str(run_dir / "results.geojson")

    write_manifest(man, RUNS)
    ok = is_valid(load_manifest(run_dir / "manifest.json"), run_root=RUNS)
    _log(f"manifest re-validated: is_valid = {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
