"""
FloodSight — End-to-End Simulation Pipeline
===========================================
M1: Hazard ingest & dam geometry
M2: DEM stage-storage V(h)
M3: Breach ensemble (Froehlich, Von Thun, MacDonald)
M4: 2D shallow-water wave routing  + Ritter benchmark
M5: Population & infrastructure exposure
M6: Time-varying OSM road-graph isolation
M7: Village priority ranking
M8: Export to .shp, .kml, CAP alert JSON, GeoTIFF

Provenance
----------
Every village row carries a ``provenance`` label saying where its numbers came
from, and the label is set where the value is produced. Nothing in this file
invents a value to fill a gap: a village the simulation does not wet reports
``inundated: false`` with null metrics, and a village with no road in the OSM
graph reports a null isolation time rather than an estimate.

An earlier version of this pipeline computed isolation as
``water_arrival - a 10-35 minute constant`` and labelled it "COMPUTED LIVE".
It also had a fallback branch that invented depths and arrival times for
villages the flood never reached. Both are gone. If a number is not computed,
it is absent.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter, zoom
from affine import Affine
from PIL import Image
import rasterio
from rasterio import transform as rtransform
from rasterio.features import shapes
import geopandas as gpd
import osmnx as ox
from shapely.geometry import Polygon, mapping, shape
from shapely.ops import unary_union, transform as shp_transform
from pyproj import Transformer

# Add project root to python path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.provenance import Provenance, LABELS, worst
from src.m3_breach import DamGeometry
from src.m2_geometry.fill import build_stage_storage
from src.m3_breach.ensemble import get_hydrographs
from src.m4_solvers.swe_2d import run_2d_swe_simulation
from src.m4_solvers.validation import run_ritter_benchmark, mass_balance
from src.m5_exposure.exposure import compute_village_exposure
from src.m6_isolation.isolation import (compute_isolation_times,
                                        emit_road_cut_timeline,
                                        THRESH_CAR)
from src.m7_ranking.ranker import rank_villages, RankWeights
from src.m8_outputs.exporters import export_shp, export_kml, export_cap_json
from src import m10_validation as m10

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("floodsight.pipeline")

DATA_DIR = Path(__file__).resolve().parent / "data"

# Depth classes for the map [m]. Phase 5 replaces the cell-polygon vectoriser.
THRESHOLDS = [0.3, 1.0, 2.0, 3.0]


def _write_depth_raster(depth: np.ndarray, path: Path, crs, transform) -> Path:
    """Write one depth grid as a GeoTIFF. M6 samples these per timestep."""
    with rasterio.open(
        path, "w", driver="GTiff",
        height=depth.shape[0], width=depth.shape[1], count=1,
        dtype="float32", crs=crs, transform=transform, compress="lzw",
    ) as dst:
        dst.write(depth.astype(np.float32), 1)
    return path


def _depth_to_geojson(depth_grid: np.ndarray, transform, t_s: float, to_wgs84) -> dict:
    """
    Convert a depth snapshot to clean, continuous GeoJSON depth-class polygons
    using rasterio vectorisation.
    """
    # The solver grid remains untouched. This higher-resolution, lightly
    # blurred copy is only for display, preventing the map from exposing each
    # coarse DEM cell as a hard square.
    # sigma is in DISPLAY pixels, so it must scale with display_scale: a blur
    # smaller than one source cell cannot cross a one-cell staircase step, which
    # is why a fixed sigma left the grid edges visible.
    display_scale = 4
    display_depth = gaussian_filter(
        zoom(depth_grid.astype(np.float32), display_scale, order=1),
        sigma=display_scale * 0.9,
    )
    display_transform = transform * Affine.scale(1 / display_scale, 1 / display_scale)
    features = []
    for cls, lo in enumerate(THRESHOLDS):
        hi = THRESHOLDS[cls + 1] if cls + 1 < len(THRESHOLDS) else 1e6
        mask = (display_depth >= lo) & (display_depth < hi)
        if not mask.any():
            continue
        # 8-connectivity: the 4-connected default splits a diagonal wet band
        # into a staircase of disconnected squares.
        raw_shapes = shapes(mask.astype(np.uint8), mask=mask,
                            transform=display_transform, connectivity=8)
        polys = [shape(g) for g, val in raw_shapes if val == 1]
        if polys:
            merged = unary_union(polys)
            # Collapse the remaining quarter-cell steps into straight runs.
            # buffer(+r).buffer(-r) was a morphological closing: it fills
            # concave notches but leaves the convex staircase corners -- the
            # ones you actually see -- untouched. Douglas-Peucker with a
            # tolerance above the display grid spacing removes both.
            tol_m = abs(float(transform.a)) / 3.0
            if tol_m > 0:
                merged = merged.simplify(tol_m, preserve_topology=True)
            if not merged.is_empty:
                merged_wgs84 = shp_transform(to_wgs84, merged)
                if not merged_wgs84.is_empty:
                    features.append({
                        "type": "Feature",
                        "properties": {"depth_class": cls + 1, "depth_lo_m": lo, "t_s": float(t_s)},
                        "geometry": mapping(merged_wgs84),
                    })
    return {"type": "FeatureCollection", "features": features}


def _write_depth_preview(depth_grid: np.ndarray, path: Path, transform, to_wgs84) -> list[list[float]]:
    """Write a smooth transparent preview texture; physics still uses depth_grid."""
    display_depth = gaussian_filter(
        zoom(depth_grid.astype(np.float32), 3, order=1), sigma=1.2)
    depth = np.maximum(display_depth, 0.0)
    u = np.clip(depth / 12.0, 0.0, 1.0)[..., None]
    shallow = np.array([91, 211, 201], dtype=np.float32)
    deep = np.array([8, 76, 112], dtype=np.float32)
    rgb = (shallow * (1.0 - u) + deep * u).astype(np.uint8)
    alpha = (np.clip((depth - 0.18) / 1.1, 0.0, 1.0) * 205).astype(np.uint8)[..., None]
    Image.fromarray(np.concatenate([rgb, alpha], axis=2), "RGBA").save(path, optimize=True)

    height, width = depth_grid.shape
    west, south, east, north = rasterio.transform.array_bounds(height, width, transform)
    return [list(to_wgs84(west, north)), list(to_wgs84(east, north)),
            list(to_wgs84(east, south)), list(to_wgs84(west, south))]


def _solve_envelope_arm(payload: dict) -> np.ndarray:
    """
    Run one non-central breach arm and return ONLY its maximum-depth grid.

    Runs in a worker process. The non-central arms exist solely to build the
    ensemble envelope -- M5, M6 and M7 all read the central arm -- so the worker
    returns a single 2-D float32 grid (~0.7 MB) rather than pickling the whole
    SimulationResult with its 13 depth snapshots back across the process
    boundary.

    Module level, not a closure: Windows spawns rather than forks, so anything
    handed to a process pool has to be importable by name.
    """
    from src.m4_solvers.swe_2d import run_2d_swe_simulation as _run
    res = _run(
        elevation_grid=payload["elev"], dx_m=payload["dx"], dy_m=payload["dy"],
        inflow_x_idx=payload["ix"], inflow_y_idx=payload["iy"],
        hydrograph_t_s=payload["t_s"], hydrograph_Q_m3s=payload["Q"],
        total_duration_s=payload["dur"], save_interval_s=payload["dur"],
        manning_n=payload["manning"], scenario_name=payload["name"],
    )
    return res.max_depth_grid.astype(np.float32)


def execute_full_simulation(
    dam_name: str = "Phutkal River Landslide Dam 2015",
    scenario_key: str = "phutkal",
    wse_m: float = 3850.0,
    failure_mode: str = "overtopping",
    reservoir_fill: float = 0.9,
    out_dir: str | Path = "data/scenarios/phutkal_real",
    total_duration_s: float = 7200.0,
    save_interval_s: float = 300.0,
    allow_synthetic: bool = False,
    coarsen: int = 1,
    progress_cb=None,
    custom_dem_path: str | Path | None = None,
    crest_length_m: float | None = None,
    dam_type: str | None = None,
    spillway_capacity_m3s: float | None = None,
    lulc_raster_path: str | Path | None = None,
    population_csv: str | Path | None = None,
) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    t_wall = time.time()

    # ── Progress reporting ────────────────────────────────────────────────────
    # Stage spans are wall-time shares taken from a profiled run, not guesses.
    # The 2D solver is the overwhelming majority of a run -- and it is now run
    # three times, once per breach arm -- so it owns most of the bar. Splitting
    # the bar evenly across modules would leave it frozen for ten minutes and
    # then jump, which is exactly the "it never seems to end" problem.
    _STAGES = {
        "terrain":  (0.00, 0.03, "Loading terrain"),
        "geometry": (0.03, 0.05, "Impoundment geometry"),
        "breach":   (0.05, 0.07, "Breach ensemble"),
        "solver":   (0.07, 0.88, "2D shallow-water solver"),
        "exposure": (0.88, 0.92, "Population & building exposure"),
        "roads":    (0.92, 0.97, "Road isolation"),
        "outputs":  (0.97, 1.00, "Ranking & exports"),
    }

    def _report(stage: str, sub: float = 0.0, detail: str = "") -> None:
        """Report overall fraction complete. Must never raise into the run."""
        if progress_cb is None:
            return
        lo, hi, label = _STAGES.get(stage, (0.0, 1.0, stage))
        frac = lo + (hi - lo) * min(max(sub, 0.0), 1.0)
        try:
            progress_cb({"stage": stage, "label": label,
                         "frac": round(frac, 4), "detail": detail,
                         "elapsed_s": round(time.time() - t_wall, 1)})
        except Exception:                                   # noqa: BLE001
            pass

    _report("terrain", 0.0, "fetching DEM")

    logger.info("=" * 60)
    logger.info("FloodSight — %s | mode: %s", dam_name, failure_mode)
    logger.info("=" * 60)

    # ── M1/M2: terrain ────────────────────────────────────────────────────────
    from src.data_fetcher import get_dem
    from src.m2_geometry.dem_utils import condition_dem, compute_hydro_surfaces, prepare_custom_dem

    if custom_dem_path and Path(custom_dem_path).exists():
        dem_path, terrain_prov = prepare_custom_dem(custom_dem_path, out_dir=out_dir)
        logger.info("M1: Ingested high-resolution custom DEM from %s", custom_dem_path)
    else:
        dem_path, terrain_prov = get_dem(scenario_key, allow_synthetic=allow_synthetic)
    if terrain_prov is Provenance.SYNTHETIC_TERRAIN:
        logger.warning("TERRAIN PROVENANCE: %s — depths and timings are illustrative",
                       LABELS[terrain_prov.value])

    # Phase 3, Item 5: HAND + Flow Accumulation
    # HAND and flow accumulation are a terrain *derivative*: written to disk for
    # later use, consumed by nothing in this pipeline today. They must therefore
    # never be able to take the run down with them.
    #
    # They currently do fail: pysheds 0.5 calls np.in1d, which NumPy removed in
    # 2.0 (this environment is on 2.5.2), so grid.accumulation() raises
    # AttributeError and every scenario died at this line. Fixing that properly
    # means either pysheds > 0.5 or numpy < 2 — a dependency decision, not
    # something to paper over by editing site-packages. Until then the run
    # continues without the surfaces and says so.
    acc_path = hand_path = None
    try:
        acc_path, hand_path = compute_hydro_surfaces(dem_path,
                                                     out_dir=dem_path.parent)
    except Exception as exc:                              # noqa: BLE001
        logger.warning("M2: HAND / flow accumulation unavailable (%s) — "
                       "continuing without them; nothing downstream reads them",
                       exc)

    with rasterio.open(dem_path) as src:
        raw_elev = src.read(1)
        transform = src.transform
        crs = src.crs
        nodata = src.nodata

    dem_elev, _repaired = condition_dem(raw_elev, nodata=nodata)

    # Optional coarsening so a full-resolution Himalayan tile stays demoable.
    if coarsen > 1:
        dem_elev = dem_elev[::coarsen, ::coarsen]
        transform = transform * rasterio.Affine.scale(coarsen, coarsen)
        logger.info("M2: DEM coarsened %dx for speed", coarsen)

    ny, nx_grid = dem_elev.shape
    if crs.is_projected:
        dx_m = abs(transform.a)
        dy_m = abs(transform.e)
    else:
        # A geographic DEM has no metric grid; the solver assumes one.
        raise ValueError(
            f"DEM CRS {crs} is geographic. Reproject to UTM first — the solver "
            "works in metres and feeding it degrees is a silent unit error."
        )
    logger.info("M2: DEM %dx%d  dx=%.1f m dy=%.1f m  elev %.0f-%.0f m  (%s)",
                nx_grid, ny, dx_m, dy_m,
                float(dem_elev.min()), float(dem_elev.max()), crs)
    cell_size_m = round((dx_m + dy_m) / 2.0, 1)  # expose to frontend for display
    logger.info("M2: effective cell size %.1f m (coarsen=%d)", cell_size_m, coarsen)

    # Every output meant for a WGS84 consumer (the map, .kml, CAP JSON)
    # reprojects through this. Built once here since it is used both for the
    # per-timestep flood snapshots and, further down, for the ranked results.
    to_wgs84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform

    _report("geometry", 0.0, "locating the blockage on the river")

    from src.data_fetcher import SCENARIOS
    sc_cfg = SCENARIOS.get(scenario_key, {})

    # Breach location in DEM coordinates — needed as the fill seed as well as
    # the inflow point, so it is resolved once, here.
    from src.m2_geometry.dem_utils import snap_to_thalweg
    from src.data_fetcher import build_rivers

    b_lon, b_lat = sc_cfg.get("breach_lon"), sc_cfg.get("breach_lat")
    to_dem = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    bx, by = to_dem.transform(b_lon, b_lat)

    # Check if scenario defines an explicit metric breach centerline
    has_explicit_breach = "breach_centerline_utm" in sc_cfg
    if has_explicit_breach:
        from shapely.geometry import LineString
        from src.m2_geometry.dem_utils import carve_breach_geometry, condition_gorge_thalweg
        centerline = LineString(sc_cfg["breach_centerline_utm"])
        b_width = sc_cfg.get("breach_width_m", 130.0)
        z_bed = sc_cfg.get("thalweg_m", 180.0)
        dem_elev, n_carved = carve_breach_geometry(
            dem_elev, transform, centerline, breach_width_m=b_width,
            breach_invert_up_m=z_bed + 5.0, breach_invert_down_m=z_bed,
        )
        logger.info("M2: Carved physical breach corridor (%d cells lowered)", n_carved)
        
        # Inflow cell is the upstream entrance of the breach
        bx, by = sc_cfg["breach_centerline_utm"][0]
        thalweg_z = z_bed
        
        if scenario_key == "annamayya":
            gx, gy = to_dem.transform(79.0050, 14.2315)
            dem_elev, n_cond = condition_gorge_thalweg(dem_elev, transform, [(gx, gy, 176.0)])
            if n_cond:
                logger.info("M2: Conditioned gorge thalweg pinch at Cheyyeru gorge exit")
    else:
        # Put the breach on the river before looking for the valley floor. A bare
        # "lowest cell within a radius" search finds whatever local minimum is
        # nearest, which on this tile is a plateau ~800 m above the gorge.
        rivers = build_rivers(scenario_key)
        river_pts = np.empty((0, 2))
        if len(rivers):
            coords = []
            for geom in rivers.to_crs(crs).geometry:
                parts = geom.geoms if geom.geom_type == "MultiLineString" else [geom]
                for part in parts:
                    coords.extend(part.coords)
            river_pts = np.asarray(coords, dtype=float)

        if len(river_pts):
            rr, cc = rtransform.rowcol(transform, river_pts[:, 0], river_pts[:, 1])
            rr = np.clip(np.asarray(rr), 0, ny - 1)
            cc = np.clip(np.asarray(cc), 0, nx_grid - 1)
            river_z = dem_elev[rr, cc]

            # Put the breach on the river channel without jumping miles downstream.
            # If a digitized river vertex is nearby (<1000m), snap to the closest one.
            dists = np.hypot(river_pts[:, 0] - bx, river_pts[:, 1] - by)
            near_mask = dists < 1000.0
            if near_mask.any():
                bi = int(np.argmin(dists))
                bx, by = float(river_pts[bi, 0]), float(river_pts[bi, 1])
                logger.info("M2: blockage placed on river vertex at %.0f m (moved %.0f m)", river_z[bi], dists[bi])
            else:
                near_wide = dists < 3000.0
                if near_wide.any():
                    bi = np.where(near_wide)[0][int(np.argmin(river_z[near_wide]))]
                    bx, by = float(river_pts[bi, 0]), float(river_pts[bi, 1])
                    logger.info("M2: blockage placed on valley stem at %.0f m", river_z[bi])
        else:
            logger.warning("M2: no OSM waterway in this AOI — the blockage cannot be "
                           "placed on a river and the fill may be unconfined")

        # A short search only corrects DEM-vs-OSM misregistration.
        bx, by, thalweg_z, _moved = snap_to_thalweg(dem_elev, transform, bx, by,
                                                    search_radius_m=300.0)

    cascade_cfg = sc_cfg.get("cascade")  # None for non-cascade scenarios

    # ── M3: breach hydrograph (cascade-aware) ─────────────────────────────────
    # If the scenario declares a `cascade` block, run the physics-based 1D
    # routing engine.  Otherwise fall back to the standard DEM-fill + Froehlich
    # ensemble.  Annamayya is handled by the same path since its `cascade` dict
    # is already embedded in data_fetcher.SCENARIOS; the Annamayya-specific
    # function is used below for backward-compat evidence anchoring.
    if cascade_cfg is not None or scenario_key == "annamayya":
        from src.m3_breach.cascade import simulate_generic_cascade, simulate_annamayya_cascade
        from src.m3_breach.ensemble import Hydrograph
        from src.m3_breach import BreachParams

        total_duration_s = max(total_duration_s, 14400.0)

        if scenario_key == "annamayya":
            # Use the evidence-anchored Annamayya wrapper (Annamayya's cascade
            # config is also embedded in the function to preserve audit trail)
            _label = "Pincha -> Annamayya Cascade Routing & Froehlich Breach Ensemble"
            _report("breach", 0.0, _label)
            res_cent = simulate_annamayya_cascade(total_duration_s=total_duration_s, dt_s=30.0, breach_tier="central")
            res_pess = simulate_annamayya_cascade(total_duration_s=total_duration_s, dt_s=30.0, breach_tier="pessimistic")
            res_opti = simulate_annamayya_cascade(total_duration_s=total_duration_s, dt_s=30.0, breach_tier="optimistic")
            _pess_label = "Froehlich Upper / 336m Structural Envelope"
        else:
            # Generic cascade: driven by the scenario's `cascade` config block
            _up = cascade_cfg.get("upstream", {})
            _label = (
                f"{_up.get('classification','?')} upstream surge -> "
                f"Muskingum 1D -> {scenario_key} reservoir -> Froehlich ensemble"
            )
            _report("breach", 0.0, _label)
            res_cent = simulate_generic_cascade(cascade_cfg, total_duration_s=total_duration_s, dt_s=30.0, breach_tier="central")
            res_pess = simulate_generic_cascade(cascade_cfg, total_duration_s=total_duration_s, dt_s=30.0, breach_tier="pessimistic")
            res_opti = simulate_generic_cascade(cascade_cfg, total_duration_s=total_duration_s, dt_s=30.0, breach_tier="optimistic")
            _pess_label = "Froehlich (2008) upper envelope"

        def _make_hg(res, method_label):
            return Hydrograph(
                t_s=res.t_s, Q_m3s=res.q_breach_m3s,
                params=BreachParams(
                    method=method_label,
                    breach_width_m=res.ensemble_metadata["breach_width_m"],
                    side_slope_hv=1.0,
                    formation_time_h=res.ensemble_metadata["formation_time_min"] / 60.0,
                    peak_discharge_m3s=res.ensemble_metadata["peak_breach_q_m3s"],
                ),
            )

        cent = _make_hg(res_cent, "Froehlich (2008) Cascade")
        pess = _make_hg(res_pess, _pess_label)
        opti = _make_hg(res_opti, "Froehlich (2008) Lower Bound")

        cascade_audit = {
            "scenario":                        scenario_key,
            "method":                          "Generic 1D Muskingum Cascade + Froehlich 2008",
            "upstream_classification":         res_cent.ensemble_metadata.get("upstream_classification", "?"),
            "routing_classification":          res_cent.ensemble_metadata.get("routing_classification", "?"),
            "reservoir_classification":        res_cent.ensemble_metadata.get("reservoir_classification", "?"),
            "initial_reservoir_storage_mcm":   round(res_cent.v_initial_m3 / 1e6, 2),
            "total_catchment_inflow_volume_mcm": round(res_cent.v_inflow_total_m3 / 1e6, 2),
            "total_breach_outflow_volume_mcm": round(res_cent.v_breach_total_m3 / 1e6, 2),
            "total_spillway_outflow_volume_mcm": round(res_cent.v_spill_total_m3 / 1e6, 2),
            "final_storage_mcm":               round(res_cent.v_final_m3 / 1e6, 2),
            "mass_conservation_error_pct":     round(res_cent.mass_error_pct, 4),
            "test_11_volume_plausibility_passed": res_cent.volume_plausibility_passed,
            "breach_geometry_type":            "MODEL RECONSTRUCTION",
            "breach_geometry_source":          "empirical breach formula (Froehlich 2008)",
        }
        if scenario_key == "annamayya":
            cascade_audit["reference_structural_upper_bound_m"] = 336.0

        with open(out_dir / "cascade_audit.json", "w", encoding="utf-8") as f:
            json.dump(cascade_audit, f, indent=2)

        with open(out_dir / "hydrograph.json", "w", encoding="utf-8") as f:
            json.dump({
                "pessimistic": {"t_s": pess.t_s.tolist(), "Q_m3s": pess.Q_m3s.tolist(),
                                "Q_p": float(pess.params.peak_discharge_m3s)},
                "central":     {"t_s": cent.t_s.tolist(), "Q_m3s": cent.Q_m3s.tolist(),
                                "Q_p": float(cent.params.peak_discharge_m3s)},
                "optimistic":  {"t_s": opti.t_s.tolist(), "Q_m3s": opti.Q_m3s.tolist(),
                                "Q_p": float(opti.params.peak_discharge_m3s)},
            }, f, indent=2)

        for label, hg in (("pessimistic", pess), ("central (Cascade)", cent), ("optimistic", opti)):
            logger.info("M3: %-24s Q_p = %8.0f m^3/s   t_f = %.2f h",
                        label, hg.params.peak_discharge_m3s, hg.params.formation_time_h)
    else:
        # ── standard single-structure breach (no upstream cascade) ─────────────
        # The impoundment is as tall as the blockage, measured from the valley
        # floor the blockage actually sits on — not from a water level carried
        # over from a different terrain model.
        dam_height = sc_cfg.get("dam_height_m", 55.0)
        wse_m = thalweg_z + dam_height
        logger.info("M2: thalweg %.0f m + blockage %.0f m -> WSE %.0f m",
                    thalweg_z, dam_height, wse_m)

        # Impounded volume from the DEM rather than a configured constant.
        stage_h = stage_V = None

        # Seed the fill upstream of the blockage: the nearest river vertex that sits
        # above the thalweg but below the water surface.
        seed_xy = (bx, by)
        if len(river_pts):
            d_to_barrier = np.hypot(river_pts[:, 0] - bx, river_pts[:, 1] - by)
            upstream = ((river_z > thalweg_z + 1.0) & (river_z < wse_m) &
                        (d_to_barrier > 200.0) & (d_to_barrier < 8000.0))
            if upstream.any():
                si = np.where(upstream)[0][int(np.argmin(d_to_barrier[upstream]))]
                seed_xy = (float(river_pts[si, 0]), float(river_pts[si, 1]))
                logger.info("M2: fill seeded %.0f m upstream at %.0f m",
                            d_to_barrier[si], river_z[si])

        try:
            geom = build_stage_storage(
                dem_path=dem_path, wse_m=wse_m, seed_xy=seed_xy,
                barrier_xy=(bx, by), barrier_radius_m=200.0,
                barrier_crest_m=wse_m + 5.0,
            )
            domain_area = nx_grid * ny * dx_m * dy_m
            if geom.area_m2 > 0.25 * domain_area:
                raise ValueError(
                    f"pool covers {100 * geom.area_m2 / domain_area:.0f}% of the "
                    "domain — implausible for a valley blockage")

            impounded_vol_m3 = geom.volume_m3 * reservoir_fill
            dam_height = geom.dam_height_m
            stage_h, stage_V = geom.stage_curve_h, geom.stage_curve_V
            vol_prov = Provenance.COMPUTED_LIVE
            logger.info("M2: V_w = %.2e m^3 from the DEM (H = %.1f m, freeboard %.1f m)",
                        impounded_vol_m3, dam_height, geom.freeboard_m)
        except Exception as exc:
            volume_mcm = sc_cfg.get("volume_mcm", 28.5)
            impounded_vol_m3 = volume_mcm * 1e6 * reservoir_fill
            dam_height = sc_cfg.get("dam_height_m", 55.0)
            vol_prov = Provenance.PROXY_DATA
            logger.warning("M2: DEM fill rejected (%s) — using the configured "
                           "volume %.1f MCM, labelled %s",
                           exc, volume_mcm, LABELS[vol_prov.value])

        # ── M3: breach ensemble ───────────────────────────────────────────────
        dam = DamGeometry(
            height_m=dam_height, volume_m3=impounded_vol_m3,
            dam_height_m=dam_height * 1.15, failure_mode=failure_mode,
            crest_length_m=crest_length_m,
            dam_type=dam_type,
            spillway_capacity_m3s=spillway_capacity_m3s,
        )
        _report("breach", 0.0, "Froehlich / Von Thun / MacDonald")
        pess, cent, opti = get_hydrographs(dam, dt_s=30.0,
                                           stage_h=stage_h, stage_V=stage_V)
        for label, hg in (("pessimistic", pess), ("central (Froehlich/CWC)", cent),
                          ("optimistic", opti)):
            logger.info("M3: %-24s Q_p = %8.0f m^3/s   t_f = %.2f h",
                        label, hg.params.peak_discharge_m3s, hg.params.formation_time_h)

        with open(out_dir / "hydrograph.json", "w", encoding="utf-8") as f:
            json.dump({
                "pessimistic": {"t_s": pess.t_s.tolist(), "Q_m3s": pess.Q_m3s.tolist(),
                                "Q_p": float(pess.params.peak_discharge_m3s)},
                "central":     {"t_s": cent.t_s.tolist(), "Q_m3s": cent.Q_m3s.tolist(),
                                "Q_p": float(cent.params.peak_discharge_m3s)},
                "optimistic":  {"t_s": opti.t_s.tolist(), "Q_m3s": opti.Q_m3s.tolist(),
                                "Q_p": float(opti.params.peak_discharge_m3s)},
            }, f, indent=2)

    # ── M4: 2D shallow water ──────────────────────────────────────────────────
    if b_lon is not None and b_lat is not None:
        # bx, by were projected above: the breach is configured in WGS84 while
        # the DEM is UTM, and indexing without projecting lands the breach in a
        # clipped corner — a working simulation of the wrong place.
        breach_iy, breach_ix = rtransform.rowcol(transform, bx, by)
        if not (0 <= breach_iy < ny and 0 <= breach_ix < nx_grid):
            raise ValueError(
                f"Breach ({b_lon}, {b_lat}) falls outside the DEM extent — "
                "check the scenario bbox."
            )
        breach_iy = int(np.clip(breach_iy, 1, ny - 2))
        breach_ix = int(np.clip(breach_ix, 1, nx_grid - 2))
        logger.info("M4: breach at (%.4f, %.4f) -> grid (%d, %d), bed %.0f m",
                    b_lon, b_lat, breach_ix, breach_iy,
                    float(dem_elev[breach_iy, breach_ix]))
    else:
        breach_ix, breach_iy = int(nx_grid * 0.18), int(ny * 0.35)
        logger.info("M4: breach location estimated (no georeference)")

    from src.m4_solvers.roughness import load_manning_from_lulc_raster
    lulc_target = Path(lulc_raster_path) if lulc_raster_path else (DATA_DIR / "landcover" / f"{scenario_key}_lulc.tif")
    if lulc_target.exists():
        try:
            manning_grid = load_manning_from_lulc_raster(lulc_target, target_shape=dem_elev.shape)
            logger.info("M4: 2D roughness grid generated directly from LULC classes (%s)", lulc_target)
        except Exception as exc:
            logger.warning("M4: LULC roughness mapping failed (%s) — falling back to elevation/pop banding", exc)
            thalweg_m = sc_cfg.get("thalweg_m", float(dem_elev.min()))
            manning_grid = np.where(
                dem_elev < thalweg_m + 25.0, 0.032,
                np.where(dem_elev < thalweg_m + 80.0, 0.055, 0.085),
            ).astype(np.float64)
    else:
        thalweg_m = sc_cfg.get("thalweg_m", float(dem_elev.min()))
        manning_grid = np.where(
            dem_elev < thalweg_m + 25.0, 0.032,
            np.where(dem_elev < thalweg_m + 80.0, 0.055, 0.085),
        ).astype(np.float64)

    # Height above the thalweg is a proxy for land cover, and it fails in the
    # one place it matters most: a coastal city sits at low elevation, so Derna
    # is banded as open channel (n = 0.032) when built-up terrain is 0.06-0.15.
    # Under-roughening the city lets the flood flush through instead of ponding
    # and spreading, which biases the extent low exactly where the observed
    # misses are.
    #
    # The population grid is the land-cover signal already on disk, aligned to
    # the DEM cell for cell by fetch_population(). Where people live is built
    # up. This is a proxy, not a land-cover classification -- published work on
    # this event derived n from ESA WorldCover -- but it is the honest use of
    # data already present, and it degrades to a clean no-op when the
    # population raster is missing or empty rather than inventing roughness.
    urban_n = float(sc_cfg.get("urban_manning_n", 0.08))
    try:
        pop_path = DATA_DIR / "population" / f"{scenario_key}_ghspop.tif"
        if pop_path.exists():
            with rasterio.open(pop_path) as psrc:
                pop = psrc.read(1)
            total_pop = float(np.nansum(pop))
            # fetch_population() aligns the raster to the FULL-resolution DEM,
            # but dem_elev has already been coarsened. Reduce the urban mask by
            # the same factor with a max, not a stride: subsampling would drop
            # every settlement that happens to fall between sampled rows.
            urban_full = np.nan_to_num(pop) >= 1.0
            if coarsen > 1 and urban_full.size:
                from scipy.ndimage import maximum_filter
                urban_full = maximum_filter(
                    urban_full, size=coarsen)[::coarsen, ::coarsen]
            urban = urban_full[:dem_elev.shape[0], :dem_elev.shape[1]]

            if urban.shape == dem_elev.shape and total_pop > 0.0:
                # GHS-POP is people per ~90 m cell; a handful per cell already
                # means settlement rather than open ground.
                manning_grid = np.where(urban, urban_n, manning_grid)
                logger.info("M4: roughness — %d urban cells (%.1f%%) set to "
                            "n=%.3f from GHS-POP", int(urban.sum()),
                            100.0 * urban.sum() / urban.size, urban_n)
            else:
                logger.warning(
                    "M4: population raster unusable for roughness "
                    "(mask %s vs DEM %s, total %.1f) — falling back to "
                    "elevation banding alone. If total is 0, the GHS-POP tile "
                    "for this scenario has not been fetched yet.",
                    urban.shape, dem_elev.shape, total_pop)
    except Exception as exc:                                  # noqa: BLE001
        logger.warning("M4: urban roughness skipped (%s: %s)",
                       type(exc).__name__, exc)

    # We run all 3 arms to build an envelope and convey uncertainty, but downstream
    # (M5, M6) currently process the central arm to save time.
    arms_to_run = [("central", cent), ("pessimistic", pess), ("optimistic", opti)]
    sim_results = {}
    envelope_grid = np.zeros_like(dem_elev, dtype=np.float32)

    # The three arms are independent solves, so running them one after another
    # made a run take three times as long as it needed to for no extra
    # information. The two envelope-only arms are dispatched to worker processes
    # and the central arm -- the one everything downstream actually reads -- runs
    # here, so its progress is still reported live. Wall time for M4 becomes
    # roughly one arm instead of three.
    from concurrent.futures import ProcessPoolExecutor

    def _central_progress(t_s: float, total_s: float) -> None:
        _report("solver", t_s / max(total_s, 1e-9),
                f"central arm — T+{t_s / 60.0:.1f} of {total_s / 60.0:.0f} min "
                f"simulated (2 envelope arms in parallel)")

    _payload = lambda name, hg: {                       # noqa: E731
        "elev": dem_elev, "dx": dx_m, "dy": dy_m,
        "ix": breach_ix, "iy": breach_iy,
        "t_s": hg.t_s, "Q": hg.Q_m3s,
        "dur": total_duration_s, "manning": manning_grid,
        "name": f"{scenario_key}_{name}",
    }
    side_arms = [(n, hg) for n, hg in arms_to_run if n != "central"]

    pool = futures = None
    try:
        pool = ProcessPoolExecutor(max_workers=max(1, len(side_arms)))
        futures = {n: pool.submit(_solve_envelope_arm, _payload(n, hg))
                   for n, hg in side_arms}
        logger.info("M4: %d envelope arms dispatched to worker processes",
                    len(futures))
    except Exception as exc:                                # noqa: BLE001
        logger.warning("M4: process pool unavailable (%s) — running the "
                       "envelope arms in series", exc)
        if pool is not None:
            pool.shutdown(wait=False)
        pool = futures = None

    logger.info("M4: Routing central breach arm...")
    sim_results["central"] = run_2d_swe_simulation(
        elevation_grid=dem_elev, dx_m=dx_m, dy_m=dy_m,
        inflow_x_idx=breach_ix, inflow_y_idx=breach_iy,
        hydrograph_t_s=cent.t_s, hydrograph_Q_m3s=cent.Q_m3s,
        total_duration_s=total_duration_s, save_interval_s=save_interval_s,
        manning_n=manning_grid, scenario_name=f"{scenario_key}_central",
        progress_cb=_central_progress,
    )
    envelope_grid = np.maximum(envelope_grid,
                               sim_results["central"].max_depth_grid)

    for arm_name, hg in side_arms:
        try:
            if futures is not None:
                grid = futures[arm_name].result()
            else:
                grid = _solve_envelope_arm(_payload(arm_name, hg))
            envelope_grid = np.maximum(envelope_grid, grid)
            logger.info("M4: %s arm folded into the envelope", arm_name)
        except Exception as exc:                            # noqa: BLE001
            # An envelope arm is uncertainty decoration. Losing one must not
            # lose the run; it is reported so the band is known to be partial.
            logger.warning("M4: %s arm failed (%s) — envelope is built from "
                           "fewer arms and understates the spread",
                           arm_name, exc)
    if pool is not None:
        pool.shutdown(wait=True)

    sim_res = sim_results["central"]

    # Max-depth raster (central arm)
    max_depth_tif = _write_depth_raster(sim_res.max_depth_grid,
                                        out_dir / "max_depth.tif", crs, transform)
    logger.info("M4: max depth raster (central) -> %s", max_depth_tif)

    # Envelope raster (max across all arms)
    envelope_tif = _write_depth_raster(envelope_grid,
                                       out_dir / "envelope.tif", crs, transform)
    
    # Vectorize envelope for frontend display
    envelope_geojson_path = out_dir / "envelope.geojson"
    with open(envelope_geojson_path, "w", encoding="utf-8") as f:
        json.dump(_depth_to_geojson(envelope_grid, transform, 0.0, to_wgs84), f)
    
    logger.info("M4: ensemble extent envelope -> %s (and .geojson)", envelope_tif)

    # Per-timestep depth rasters (central arm) — M6 samples these to cut the road graph.
    depth_dir = out_dir / "depth_rasters"
    depth_dir.mkdir(exist_ok=True)
    raster_stack: list[tuple[float, str]] = []
    for i, (t_s, dg) in enumerate(zip(sim_res.times_s, sim_res.depth_grids)):
        p = _write_depth_raster(dg, depth_dir / f"depth_{i:03d}.tif", crs, transform)
        raster_stack.append((float(t_s), str(p)))
    logger.info("M4: %d per-timestep depth rasters (central) -> %s", len(raster_stack), depth_dir)

    # GeoJSON snapshots for the map scrubber (central arm)
    snapshots_dir = out_dir / "snapshots"
    snapshots_dir.mkdir(exist_ok=True)
    preview_dir = out_dir / "preview_rasters"
    preview_dir.mkdir(exist_ok=True)

    # ── Stage 1: Pre-Breach Lake Formation & Impoundment Rise ─────────────────
    # Generates temporal snapshots before breach (T < 0) showing water backing
    # up behind the landslide blockage / dam crest.
    from src.m2_geometry.fill import compute_lake_depth_grids
    lake_frames = []
    lake_meta = {"scenario": scenario_key, "stages": []}
    try:
        if scenario_key == "annamayya":
            lake_depths = []
        else:
            lake_fractions = (0.25, 0.50, 0.75, 0.95)
            lake_dt_mins = [-120.0, -60.0, -30.0, -10.0]
            lake_depths = compute_lake_depth_grids(
                dem_array=dem_elev,
                transform=transform,
                seed_xy=seed_xy,
                wse_m=wse_m,
                fractions=lake_fractions,
                barrier_xy=(bx, by),
                barrier_radius_m=200.0,
                barrier_crest_m=wse_m + 5.0,
            )
        for k, lk in enumerate(lake_depths):
            t_min = lake_dt_mins[k] if k < len(lake_dt_mins) else -10.0
            t_s = t_min * 60.0
            fp = snapshots_dir / f"frame_pre_{k:02d}.geojson"
            preview_path = preview_dir / f"frame_pre_{k:02d}.png"
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(_depth_to_geojson(lk["depth_grid"], transform, t_s, to_wgs84), f)
            preview_bounds = _write_depth_preview(lk["depth_grid"], preview_path, transform, to_wgs84)
            fr_entry = {
                "t_s": t_s,
                "t_min": t_min,
                "stage": "lake_formation",
                "phase_title": f"Lake Formation ({int(lk['fraction']*100)}% Capacity)",
                "volume_mcm": round(lk["volume_m3"] / 1e6, 2),
                "area_km2": round(lk["area_m2"] / 1e6, 2),
                "level_m": round(lk["level_m"], 1),
                "path": str(fp),
                "preview_path": str(preview_path),
                "preview_bounds": preview_bounds,
            }
            lake_frames.append(fr_entry)
            lake_meta["stages"].append(fr_entry)
        with open(out_dir / "lake_formation.json", "w", encoding="utf-8") as f:
            json.dump(lake_meta, f, indent=2)
        logger.info("M2: Generated %d pre-breach lake formation snapshots (T-120m to T-10m)", len(lake_frames))
    except Exception as exc:
        logger.warning("M2: Pre-breach lake snapshots omitted (%s)", exc)
        lake_frames = []

    post_breach_frames = []
    for i, (t_s, dg) in enumerate(zip(sim_res.times_s, sim_res.depth_grids)):
        fp = snapshots_dir / f"frame_{i:03d}.geojson"
        preview_path = preview_dir / f"frame_{i:03d}.png"
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(_depth_to_geojson(dg, transform, t_s, to_wgs84), f)
        preview_bounds = _write_depth_preview(dg, preview_path, transform, to_wgs84)
        post_breach_frames.append({
            "t_s": t_s,
            "t_min": round(t_s / 60.0, 1),
            "stage": "breach_and_propagation",
            "phase_title": "Dam Breach (T+0 min)" if i == 0 else f"Flood Propagation (T+{round(t_s / 60.0, 1)} min)",
            "path": str(fp),
            "preview_path": str(preview_path),
            "preview_bounds": preview_bounds,
        })
    
    snapshot_frames = lake_frames + post_breach_frames
    snapshots_index_path = out_dir / "snapshots_index.json"
    with open(snapshots_index_path, "w", encoding="utf-8") as f:
        json.dump(snapshot_frames, f, indent=2)

    # Mass balance — an honest closure figure beats a confident depth number.
    mb = mass_balance(sim_res, cent.t_s, cent.Q_m3s)
    logger.info("M4: mass balance (central) — injected %.3e m^3, stored %.3e m^3, "
                "unaccounted %.1f%% (outflow + numerical)",
                mb["injected_volume_m3"], mb["stored_volume_m3"],
                mb["relative_error"] * 100.0)

    # ── M5: exposure ──────────────────────────────────────────────────────────
    from src.data_fetcher import (build_villages, build_facilities,
                                  build_buildings, download_osm_road_network)

    villages_gdf   = build_villages(scenario_key)
    facilities_gdf = build_facilities(scenario_key)
    buildings_gdf  = build_buildings(scenario_key)
    logger.info("M5: %d settlements, %d facilities, %d building footprints (OSM)",
                len(villages_gdf), len(facilities_gdf), len(buildings_gdf))

    from src.data_fetcher import fetch_population
    from src.m5_exposure.exposure import load_custom_population_csv
    try:
        ghspop = fetch_population(scenario_key)
    except Exception as exc:
        logger.warning("GHS-POP unavailable (%s) — population will be PROXY_DATA", exc)
        ghspop = None

    custom_pop_map = None
    if population_csv and Path(population_csv).exists():
        try:
            custom_pop_map = load_custom_population_csv(population_csv)
            logger.info("M5: Loaded %d surveyed local population records from %s",
                        len(custom_pop_map), population_csv)
        except Exception as exc:
            logger.warning("M5: Failed to load custom population CSV (%s)", exc)

    _report("exposure", 0.0, "sampling population and buildings")
    exposure_gdf = compute_village_exposure(
        inundation_raster=max_depth_tif,
        village_polygons=villages_gdf,
        ghspop_raster=ghspop,
        buildings_gdf=buildings_gdf if len(buildings_gdf) else None,
        facilities_gdf=facilities_gdf if len(facilities_gdf) else None,
        depth_threshold_m=THRESH_CAR,
        custom_population=custom_pop_map,
    )

    # ── M6: road isolation ────────────────────────────────────────────────────
    _report("roads", 0.0, "cutting the road graph per timestep")
    roads_file = download_osm_road_network(scenario_key)
    G = ox.load_graphml(roads_file)

    isolation_gdf = compute_isolation_times(
        G=G, raster_stack=raster_stack, village_gdf=villages_gdf,
        max_depth_raster=max_depth_tif, threshold_m=THRESH_CAR,
    )

    # ── Road timeline (the differentiator made visible) ────────────────────────
    roads_timeline_path = out_dir / "roads_timeline.geojson"
    try:
        emit_road_cut_timeline(
            G=G, raster_stack=raster_stack,
            out_path=roads_timeline_path, threshold_m=THRESH_CAR,
        )
        logger.info("M6: road timeline → %s", roads_timeline_path)
    except Exception as exc:
        logger.warning("M6: road timeline failed (%s) — continuing without it", exc)
        roads_timeline_path = None

    # ── Arrival-time raster ────────────────────────────────────────────────────
    # One-pass reduction over the depth raster stack: for each cell, the first
    # timestep at which depth >= THRESH_CAR. This single raster answers
    # "where is the flood moving and how fast" in a static frame.
    arrival_time_path = out_dir / "arrival_time.tif"
    try:
        # Build arrival-time array: shape = (ny, nx), value = t_s or NaN
        arr_time = np.full((ny, nx_grid), np.nan, dtype=np.float32)
        for t_s, raster_path in raster_stack:
            with rasterio.open(raster_path) as src:
                d = src.read(1).astype(float)
            newly_wet = (d >= THRESH_CAR) & np.isnan(arr_time)
            arr_time[newly_wet] = float(t_s) / 60.0  # store in minutes
        _write_depth_raster(arr_time, arrival_time_path, crs, transform)
        logger.info("M4+: arrival-time raster → %s  (values in minutes)", arrival_time_path)
    except Exception as exc:
        logger.warning("Arrival-time raster failed (%s) — continuing without it", exc)
        arrival_time_path = None

    # ── M7: ranking ───────────────────────────────────────────────────────────
    _report("outputs", 0.0, "ranking villages")
    ranked = rank_villages(exposure_gdf, isolation_gdf, weights=RankWeights())

    # rank_villages inherits exposure_gdf's CRS, which M5 set to the DEM's CRS
    # (UTM, since Phase 2) so it could sample the depth raster directly.
    # Reproject once, here, at the output boundary -- everything downstream
    # (results.geojson for the map, .shp, .kml, CAP JSON) is WGS84 from this
    # point on, and every internal computation upstream stayed correctly in
    # UTM metres throughout.
    if ranked.crs is not None and str(ranked.crs) != "EPSG:4326":
        ranked = ranked.to_crs("EPSG:4326")

    # ── Provenance stamping ───────────────────────────────────────────────────
    # A live solver on synthetic terrain is still synthetic; `worst` enforces it.
    def _row_provenance(row) -> str:
        return str(worst(Provenance.COMPUTED_LIVE, terrain_prov,
                         row.get("pop_provenance")))

    ranked["provenance"] = ranked.apply(_row_provenance, axis=1)
    ranked["label"] = ranked["provenance"].map(lambda p: LABELS[p])
    # Metrics that were not computed stay null. The UI renders them as a dash.
    ranked["isolation_computed"] = ranked.get(
        "road_nodes_found", True) & ranked["isolation_time_min"].notna()

    n_inund = int(ranked["inundated"].sum()) if "inundated" in ranked else 0
    total_par = int(ranked["pop_at_risk"].sum())
    total_buildings = int(ranked["buildings_flooded"].sum())
    total_loss_inr = float(ranked["loss_inr"].sum())

    results_geojson_path = out_dir / "results.geojson"
    ranked.to_file(results_geojson_path, driver="GeoJSON", encoding="UTF-8")
    logger.info("M7: results -> %s", results_geojson_path)

    # ── M8: exports ───────────────────────────────────────────────────────────
    exports_dir = out_dir / "exports"
    safe_name = dam_name.replace(" ", "_")
    shp_path = export_shp(ranked, exports_dir, safe_name)
    kml_path = export_kml(ranked, exports_dir, safe_name)
    cap_path = export_cap_json(
        ranked, dam_name,
        {"method": cent.params.method,
         "Q_p_m3s": round(cent.params.peak_discharge_m3s, 1),
         "t_f_h": round(cent.params.formation_time_h, 2)},
        exports_dir,
    )

    # ── M10: score against the observed outcome, when one exists ──────────────
    # Ritter proves the solver reproduces an analytical solution. It does not
    # prove the model reproduces a real flood. Only the Derna scenario has an
    # independently observed outcome (Copernicus EMS EMSR696) to score against;
    # every other scenario reports the absence of observed data, never a score.
    observed_block: dict = {"available": False, "scenario": scenario_key}
    agreement_path = observed_extent_path = roads_val_path = None
    if m10.available(scenario_key):
        try:
            cmp_res = m10.compare_extent(max_depth_tif, scenario_key,
                                         threshold_m=THRESH_CAR)
            agreement_path = out_dir / "validation_agreement.geojson"
            with open(agreement_path, "w", encoding="utf-8") as f:
                json.dump(m10.agreement_geojson(cmp_res, simplify_m=dx_m / 2.0), f)

            observed_extent_path = out_dir / "observed_extent.geojson"
            with open(observed_extent_path, "w", encoding="utf-8") as f:
                json.dump(m10.observed_extent_geojson(scenario_key), f)

            observed_block = {
                "available": True,
                "scenario": scenario_key,
                "overlapped": bool(cmp_res.overlapped),
                "threshold_m": THRESH_CAR,
                "extent": cmp_res.skill.as_dict(),
                "areas": cmp_res.areas_km2(),
                **(m10.describe(scenario_key) or {}),
            }
            logger.info("M10: extent — %s", cmp_res.skill.summary())

            try:
                road_skill, road_gdf = m10.compare_roads(
                    max_depth_tif, scenario_key, threshold_m=THRESH_CAR)
                roads_val_path = out_dir / "validation_roads.geojson"
                road_gdf.to_file(roads_val_path, driver="GeoJSON")
                observed_block["roads"] = road_skill.as_dict()
                logger.info("M10: %s", road_skill.summary())
            except Exception as exc:                      # noqa: BLE001
                logger.warning("M10: road validation unavailable (%s)", exc)
                observed_block["roads"] = None
        except Exception as exc:                          # noqa: BLE001
            logger.warning("M10: extent validation failed (%s)", exc)
            observed_block = {"available": False, "scenario": scenario_key,
                              "error": str(exc)}
    else:
        logger.info("M10: no observed outcome wired for '%s' — reporting its "
                    "absence rather than a score", scenario_key)

    # ── Validation: the solver is actually run against Ritter ─────────────────
    # Phase 3: Also run the SWE-SPH arm on the same benchmark, so the Solver
    # Comparison tab can show REAL results for both solvers, not fabricated curves.
    bench = run_ritter_benchmark()
    sph_result_data = None
    try:
        from src.m4_solvers.sph_swe import ritter_dam_break_sph
        # Ritter: h1=10m, t=60s, domain 4km, particle spacing 10m
        h1, t_s = 10.0, 60.0
        sph_res, sph_hsml = ritter_dam_break_sph(
            h1_m=h1, t_eval_s=t_s, domain_m=4000.0, particle_spacing_m=10.0,
        )
        # Sample SPH field at the same x positions as the FV benchmark
        import math
        g = 9.81
        c0 = math.sqrt(g * h1)
        x_grid = [(-500 + i * (1000 / 299)) for i in range(300)]
        x_arr = np.array(x_grid)
        h_sph = sph_res.sample_on(x_arr, sph_hsml).tolist()
        h_ritter = [
            h1 if xi < -c0 * t_s
            else 0.0 if xi > 2 * c0 * t_s
            else (1.0 / (9.0 * g)) * (2.0 * c0 - xi / t_s) ** 2
            for xi in x_grid
        ]
        rmse_sph = float(np.sqrt(np.mean((np.array(h_sph) - np.array(h_ritter)) ** 2)))
        sph_result_data = {
            "x": x_grid, "h": h_ritter, "h_sph": h_sph,
            "h1": h1, "t_s": t_s, "rmse_sph": rmse_sph,
        }
        logger.info("SPH Ritter RMSE = %.3f m (%d particles, %d steps, %.1f s wall)",
                    rmse_sph, sph_res.n_particles, sph_res.steps, sph_res.wall_time_s)
    except Exception as exc:
        logger.warning("SPH Ritter benchmark failed (%s) — omitting from validation", exc)

    validation = {
        "ritter": asdict(bench),
        "sph_ritter": sph_result_data,
        "mass_balance": mb,
        "terrain_provenance": str(terrain_prov),
        "observed": observed_block,
    }
    with open(out_dir / "ritter_validation.json", "w", encoding="utf-8") as f:
        json.dump(validation, f, indent=2)
    logger.info("M4: %s", bench.summary())
    if bench.detail["front_relative_error"] > 0.10:
        logger.warning("M4: wave front %.0f%% short of analytical — first-order "
                       "diffusion means arrival times read LATE",
                       bench.detail["front_relative_error"] * 100.0)

    logger.info("=" * 60)
    logger.info("Complete in %.1f s", time.time() - t_wall)
    logger.info("Villages inundated : %d / %d", n_inund, len(ranked))
    logger.info("Population at risk : %d", total_par)
    logger.info("Buildings flooded  : %d", total_buildings)
    logger.info("Direct loss est.   : Rs %s", f"{int(total_loss_inr):,}")
    logger.info("Provenance         : %s", LABELS[str(terrain_prov)])
    logger.info("=" * 60)

    return {
        "status": "done",
        "scenario_name": dam_name,
        "results_geojson": str(results_geojson_path),
        "hydrograph_json": str(out_dir / "hydrograph.json"),
        "max_depth_tif": str(max_depth_tif),
        "envelope_tif": str(envelope_tif) if 'envelope_tif' in locals() else None,
        "envelope_geojson": str(envelope_geojson_path) if 'envelope_geojson_path' in locals() else None,
        "snapshots_index": str(snapshots_index_path),
        "roads_timeline": str(roads_timeline_path) if roads_timeline_path else None,
        "validation_agreement": str(agreement_path) if agreement_path else None,
        "observed_extent": str(observed_extent_path) if observed_extent_path else None,
        "validation_roads": str(roads_val_path) if roads_val_path else None,
        "observed": observed_block,
        "arrival_time_tif": str(arrival_time_path) if arrival_time_path else None,
        "cell_size_m": cell_size_m,
        "coarsen": coarsen,
        "shp": str(shp_path), "kml": str(kml_path), "cap": str(cap_path),
        "total_par": total_par,
        "total_buildings": total_buildings,
        "total_loss_inr": total_loss_inr,
        "villages_inundated": n_inund,
        "terrain_provenance": str(terrain_prov),
        "lake_formation": str(out_dir / "lake_formation.json") if (out_dir / "lake_formation.json").exists() else None,
        "validation": validation,
    }


if __name__ == "__main__":
    from src.data_fetcher import SCENARIOS
    parser = argparse.ArgumentParser(description="FloodSight simulation pipeline")
    parser.add_argument("--dam-name", default=None)
    parser.add_argument("--scenario", default="phutkal",
                        choices=list(SCENARIOS.keys()))
    parser.add_argument("--wse", type=float, default=None)
    parser.add_argument("--failure-mode", choices=["overtopping", "piping"],
                        default="overtopping")
    parser.add_argument("--reservoir-fill", type=float, default=0.9)
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--duration", type=float, default=None)
    parser.add_argument("--coarsen", type=int, default=1,
                        help="downsample the DEM by this factor for speed")
    parser.add_argument("--offline-demo", action="store_true",
                        help="permit synthetic terrain if the DEM cannot be "
                             "fetched; output is labelled SYNTHETIC_TERRAIN")
    parser.add_argument("--custom-dem", default=None, help="Path to high-res custom DEM GeoTIFF")
    parser.add_argument("--crest-length", type=float, default=None, help="Dam crest length in meters (CWC)")
    parser.add_argument("--dam-type", choices=["earthfill", "rockfill", "concrete", "masonry"], default=None, help="Dam core material")
    parser.add_argument("--spillway-capacity", type=float, default=None, help="Controlled spillway capacity m^3/s")
    parser.add_argument("--lulc-path", default=None, help="Path to ESA WorldCover / LULC land cover GeoTIFF")
    parser.add_argument("--population-csv", default=None, help="Path to local surveyed population CSV")
    args = parser.parse_args()

    sc_info = SCENARIOS.get(args.scenario, {})
    dam_name = args.dam_name or sc_info.get("name", f"{args.scenario.capitalize()} Scenario")
    wse_m = args.wse or sc_info.get("wse_m", 3850.0)
    out_dir = args.out_dir or f"data/scenarios/{args.scenario}_real"
    duration_s = args.duration or (14400.0 if args.scenario == "annamayya" else 7200.0)

    execute_full_simulation(
        dam_name=dam_name, scenario_key=args.scenario, wse_m=wse_m,
        failure_mode=args.failure_mode, reservoir_fill=args.reservoir_fill,
        out_dir=out_dir, total_duration_s=duration_s,
        allow_synthetic=args.offline_demo, coarsen=args.coarsen,
        custom_dem_path=args.custom_dem, crest_length_m=args.crest_length,
        dam_type=args.dam_type, spillway_capacity_m3s=args.spillway_capacity,
        lulc_raster_path=args.lulc_path, population_csv=args.population_csv,
    )
