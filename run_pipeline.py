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
from src.m3_breach import DamGeometry, failure_mode_to_mechanism, require_implemented_mechanism
from src.m2_geometry.fill import build_stage_storage, level_for_volume_fraction
from src.m3_breach.ensemble import get_hydrographs
from src.m4_solvers.swe_2d import run_2d_swe_simulation
from src.m4_solvers.validation import run_ritter_benchmark
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
        initial_depth=payload.get("initial_depth"),
        hydrograph_v_ms=payload.get("v_ms"),
        inflow_direction=payload.get("direction"),
        # An arm without an opening re-supplies the impoundment it was already
        # given as `initial_depth` -- defect C1, which survived in the envelope
        # products after the central arm was fixed (audit Part V RC-5).
        breach_opening=payload.get("breach_opening"),
        upstream_cv_mask=payload.get("upstream_cv_mask"),
    )
    return res.max_depth_grid.astype(np.float32)


# ── P1 gate tolerances and the gates themselves ───────────────────────────────
# Module level, not inline in `execute_full_simulation`, so `tests/test_mass_gates.py`
# asserts against the gate the pipeline actually runs rather than a local copy of
# it. The copies it used to carry were annotated "Must match
# execute_full_simulation" and did not have to: changing the tolerance, the
# numerator or the sign here left every one of those tests green (audit Part VI
# N-6). That is the C5 shape one level up -- the gate can fail, but its test
# could not notice if it stopped being able to.
G1_TOLERANCE = 0.05     # 5 % of the impoundment
G2_TOLERANCE = 0.001    # 0.1 % of total input manufactured by clipping


def gate_g1(initial_m3: float, injected_m3: float, impounded_m3: float):
    """G1 -- volume provenance. Every cubic metre in the domain must come from
    the impoundment we claim failed. `initial + injected` is the whole supply
    side of the ledger; `impounded_m3` is what the reservoir held.

    Returns ``(ok, ratio)``. The C1 double count (pool placed as `initial_depth`
    AND re-supplied as the injected hydrograph) makes the ratio exactly 2.00.
    An under-filled DEM pool makes it far below 1.00 -- measured 0.110 on live
    phutkal at coarsen 4 (audit Part VI N-14).
    """
    supplied = float(initial_m3) + float(injected_m3)
    ratio = (supplied / impounded_m3) if (impounded_m3 or 0) > 0 else float("nan")
    return bool(np.isfinite(ratio) and abs(ratio - 1.0) <= G1_TOLERANCE), ratio


def gate_g2(clipped_m3: float, initial_m3: float, injected_m3: float):
    """G2 -- manufactured mass. Negative depths produced by the scheme are
    clamped to zero and the clamped volume booked into `clipped`, where the
    closure identity ADDS it back as if it were accounted for. It is not: it is
    water invented by the discretisation.

    Returns ``(ok, fraction_of_input)``. A NaN `clipped_m3` (a diverged solve)
    fails closed, because ``nan <= tol`` is False.
    """
    total_in = float(initial_m3) + float(injected_m3)
    frac = (float(clipped_m3) / total_in) if total_in > 0 else 0.0
    return bool(frac <= G2_TOLERANCE), frac


def execute_full_simulation(
    dam_name: str | None = None,
    scenario_key: str = "phutkal",
    wse_m: float | None = None,
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
    lulc_raster_path: str | Path | None = None,
    population_csv: str | Path | None = None,
) -> dict:
    # `wse_m` is REFUSED, not honoured, and not ignored.
    #
    # This parameter used to be accepted, validated and then silently
    # discarded: it is never read between here and the two points that
    # overwrite it -- `:549` on the cascade path (`z_crest_m`) and `:552` on
    # the non-cascade path (`thalweg_z + dam_height`, clamped to the sourced
    # crest at `:571-577`). The CLI's `--wse`, `src/api/main.py`'s request
    # field and `src/api/worker.py`'s forwarding all fed a value into that
    # void. Worse, `main.py` defaulted the field to 3850.0 and wrote the whole
    # request into the run manifest, so archived manifests RECORD a water
    # level the run never used: `101520ad...`'s request says 3850.0 while the
    # run it describes used 3805.11.
    #
    # Honouring it is not the fix. The pool level must be DERIVED from the
    # sourced `thalweg_m + dam_height_m` and BOUNDED by the sourced
    # `crest_elev_m` -- see findings_results.md, "DEFECT 1 - the pool level
    # was derived above the sourced crest", where a level 1.23 m above the
    # crest cost a 46x volume blow-out. A caller-supplied level is a level
    # floating free of its sourcing, which is the defect that work removed.
    #
    # So the silent no-op becomes a loud refusal. `frontend/index.html`
    # already removed its "Water level" box for this reason and says so.
    # Resolve the dam's name from the scenario when the caller did not give one.
    # The default used to be the literal string "Phutkal River Landslide Dam
    # 2015", so any caller that omitted it labelled its run -- and its exports,
    # and its manifest -- as Phutkal regardless of which dam it actually ran.
    # Observed 2026-09-19 on a rishiganga job submitted through the API.
    if not dam_name:
        from src.data_fetcher import SCENARIOS as _SC
        dam_name = (_SC.get(scenario_key, {}) or {}).get("name")             or f"{scenario_key} scenario"

    if wse_m is not None:
        raise ValueError(
            f"wse_m is not a settable parameter (got {wse_m!r}). The impounded "
            f"water level is DERIVED from the scenario's sourced thalweg_m + "
            f"dam_height_m (or the cascade reservoir's z_crest_m) and is then "
            f"clamped to the sourced crest_elev_m, so a supplied level would be "
            f"overwritten before first use. It was silently discarded until "
            f"2026-09-18. Pass wse_m=None, or omit it. The level the run "
            f"actually used is reported as validity.initial_wse_m."
        )

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

    # No sourced floor here, deliberately. `thalweg_m` is the DAM-SITE bed, not
    # the domain minimum, so `thalweg_m - margin` is not a domain floor: at
    # coarsen 4 it walls 34,983 cells of real terrain on rishiganga (floor
    # 2,280 m), 2,186 on phutkal, and 2,649,346 on south_lhonak, whose real
    # valley floor is 715 m against a 5,140 m thalweg. Measured 2026-09-13 --
    # do not re-add it. These domains are protected by the exact-zero wall and
    # the zero-excluded percentile inside condition_dem instead. A sourced
    # floor belongs only where the domain minimum and the thalweg are close and
    # the gap has been measured (scripts/route_annamayya.py).
    dem_elev, wall_mask = condition_dem(raw_elev, nodata=nodata)

    # Optional coarsening so a full-resolution Himalayan tile stays demoable.
    if coarsen > 1:
        dem_elev = dem_elev[::coarsen, ::coarsen]
        wall_mask = wall_mask[::coarsen, ::coarsen]
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

    # ── Geometry hard gate (Stage B) ──────────────────────────────────────────
    # validate_geometry() proves the scenario's manifest geometry (barrier,
    # breach, river, upstream/downstream seeds) actually resolves against this
    # DEM before any physics runs: barrier intersects breach zone and river,
    # the breach point sits on the river at DEM resolution, upstream/downstream
    # seeds are genuinely disconnected before failure, the breach opening
    # connects to the downstream side, and the upstream pool is confined. A
    # scenario without confirmed geometry must not fall through to the disc
    # (`barrier_xy`/`barrier_radius_m`) or to `carve_breach_geometry`'s
    # arbitrary corridor — GeometryValidationError propagates out of this
    # function uncaught (src/api/worker.py::run_manifest_file already turns
    # any propagated exception into a "failed" manifest with the message in
    # validity.reasons). This is a deliberate hard gate, not a bug to work
    # around (memory.md, "Physical rebuild Stage B — geometry gate is a hard
    # fail, decided 2026-09-11"); annamayya and south_lhonak are expected to
    # fail here today.
    #
    # Run against the raw conditioned (and possibly coarsened) DEM, BEFORE
    # `carve_breach_geometry`/`condition_gorge_thalweg` run below — carving
    # exists to open a hydraulic path through terrain the DSM can't resolve,
    # so validating after carving would hide exactly the unconfirmed-geometry
    # condition this gate exists to catch (see the `has_explicit_breach`
    # block: carving there is now conditioned on this gate having passed).
    from src.m2_geometry.validation import (GeometryValidationError,
                                            validate_geometry)
    geometry_manifest_path = DATA_DIR / "geometry" / f"{scenario_key}.json"
    if not geometry_manifest_path.exists():
        # Without this the read raises FileNotFoundError, which worker.py turns
        # into a "failed" manifest whose only reason is an OS errno and a
        # Windows path -- useless to anyone reading it. south_lhonak is the live
        # case: its geometry is authored per STRUCTURE
        # (south_lhonak_chungthang.json, south_lhonak_moraine.json) because the
        # event has two of them, so no file is ever written under the scenario
        # key itself. Name what exists rather than making the next reader go
        # looking. A GeometryValidationError here is the same hard refusal an
        # unusable manifest gets -- this is not a fallback path.
        siblings = sorted(p.stem for p in (DATA_DIR / "geometry").glob(f"{scenario_key}_*.json"))
        raise GeometryValidationError(
            f"no geometry manifest for scenario '{scenario_key}'"
            + (f" — geometry is authored per structure as {', '.join(siblings)}; "
               "this scenario needs a compound-event path that selects between "
               "them, not a manifest under its own key" if siblings else
               f" — expected {geometry_manifest_path.name}"),
            {"scenario_key": scenario_key,
             "expected_path": str(geometry_manifest_path),
             "structure_manifests": siblings})
    geometry_manifest = json.loads(geometry_manifest_path.read_text(encoding="utf-8"))
    geometry_result = validate_geometry(geometry_manifest, dem_elev, transform, crs)
    logger.info("M2: geometry gate PASSED for %s — barrier/breach/river/seeds "
                "resolved against %s", scenario_key, dem_path)

    # ── P2 (audit SS37, defect C3): put the barrier in the terrain the solver
    # integrates ─────────────────────────────────────────────────────────────
    # `fill.py` already performed exactly this operation, twice, on PRIVATE
    # COPIES — so the stage-storage curve and the lake frames saw a barrier
    # while the array handed to `run_2d_swe_simulation` never had a structure
    # in it at all. The impoundment therefore had nothing holding it back, and
    # every downstream defect follows from that: nothing for a breach to open,
    # so the discharge had to be faked as a volumetric source (C2), so the
    # reservoir had to be supplied separately from its own drainage (C1).
    #
    # Done ONCE, here, on `dem_elev` itself. The crest is the manifest's
    # sourced `crest_elev_m` (the geometry gate refuses a scenario without
    # one) — NOT `wse_m + 5.0`, which was an invented 5 m freeboard whose only
    # job was to make the seeded fill close.
    #
    # Placement matters: this runs BEFORE `open_river_outlets` and
    # `condition_flowline` so that the flowline conditioner's protect_mask is
    # protecting a barrier that actually exists, and BEFORE
    # `dem_crest_along_axis` is sampled so the crest gate sees the emplaced
    # structure rather than the bare valley.
    barrier_crest_elev_m = float(geometry_result["crest_elev_m"])
    _barrier_mask = geometry_result["barrier_mask"]
    _below = _barrier_mask & (dem_elev < barrier_crest_elev_m)
    barrier_emplacement = {
        "crest_elev_m": barrier_crest_elev_m,
        "crest_source": geometry_result["crest_elev_source"],
        "crest_classification": geometry_result["crest_elev_classification"],
        "barrier_cells": int(_barrier_mask.sum()),
        "cells_raised": int(_below.sum()),
        "max_raise_m": float((barrier_crest_elev_m - dem_elev[_below]).max()) if _below.any() else 0.0,
        "volume_emplaced_m3": float((barrier_crest_elev_m - dem_elev[_below]).sum() * dx_m * dy_m)
        if _below.any() else 0.0,
    }
    # The bed BEFORE the structure went in. P3's breach erodes down to this and
    # no further: a breach cannot cut below the valley floor the dam stands on.
    dem_natural = dem_elev.copy()
    dem_elev = np.where(_below, barrier_crest_elev_m, dem_elev)
    logger.info("M2: barrier emplaced in the SOLVER's DEM — crest %.2f m [%s], "
                "%d/%d barrier cells raised, max raise %.2f m, %.3e m^3 of structure",
                barrier_crest_elev_m, barrier_emplacement["crest_classification"],
                barrier_emplacement["cells_raised"], barrier_emplacement["barrier_cells"],
                barrier_emplacement["max_raise_m"], barrier_emplacement["volume_emplaced_m3"])

    _report("geometry", 0.0, "locating the blockage on the river")

    from src.data_fetcher import SCENARIOS
    sc_cfg = SCENARIOS.get(scenario_key, {})

    # Bound here, not only inside the breach block, so the P1 gates below can
    # always be evaluated. A run that bails before the impoundment is sized
    # must FAIL G1/G3 for want of a denominator, not crash with an
    # UnboundLocalError and lose the whole validity block.
    impounded_vol_m3: float | None = None
    dam_height: float | None = None

    # Breach location in DEM coordinates — needed as the fill seed as well as
    # the inflow point, so it is resolved once, here.
    from src.m2_geometry.dem_utils import snap_to_thalweg
    from src.data_fetcher import build_rivers

    b_lon, b_lat = sc_cfg.get("breach_lon"), sc_cfg.get("breach_lat")
    to_dem = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    bx, by = to_dem.transform(b_lon, b_lat)

    # Check if scenario defines an explicit metric breach centerline
    has_explicit_breach = "breach_centerline_utm" in sc_cfg
    # P0-2 (FS-04/FS-05): the DEM barrier's own crest, sampled BEFORE any
    # carving lowers it. carve_breach_geometry / condition_gorge_thalweg exist
    # to open a hydraulic path through terrain the 30 m DSM can't resolve — but
    # that means checking the barrier AFTER carving would hide exactly the
    # over-crest condition this gate exists to catch. Sampled along the
    # centerline itself (or a search window around the breach point when no
    # centerline is declared) because no scenario carries a `dam_axis` distinct
    # from the breach line today (FS-14) — this is the best DEM-derived
    # approximation available without surveyed structure geometry.
    dem_crest_along_axis = None
    if has_explicit_breach:
        centerline_pts = sc_cfg["breach_centerline_utm"]
        rows, cols = rtransform.rowcol(
            transform, [p[0] for p in centerline_pts], [p[1] for p in centerline_pts])
        rows = np.clip(np.asarray(rows), 0, ny - 1)
        cols = np.clip(np.asarray(cols), 0, nx_grid - 1)
        dem_crest_along_axis = float(dem_elev[rows, cols].max())
    else:
        window_m = 250.0
        wcol, wrow = ~transform * (bx, by)
        r0, r1 = max(0, int(wrow - window_m / dy_m)), min(ny, int(wrow + window_m / dy_m) + 1)
        c0, c1 = max(0, int(wcol - window_m / dx_m)), min(nx_grid, int(wcol + window_m / dx_m) + 1)
        if r1 > r0 and c1 > c0:
            dem_crest_along_axis = float(dem_elev[r0:r1, c0:c1].max())

    if has_explicit_breach:
        from shapely.geometry import LineString
        from src.m2_geometry.dem_utils import carve_breach_geometry
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
    # Rivers and river points for blockage snapping and impoundment seeding
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
    else:
        river_z = np.empty(0)

    if not has_explicit_breach:
        # Put the breach on the river before looking for the valley floor. A bare
        # "lowest cell within a radius" search finds whatever local minimum is
        # nearest, which on this tile is a plateau ~800 m above the gorge.
        if len(river_pts):
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

    # 1a: Bind wse_m and seed_xy in the common scope for BOTH cascade and
    # non-cascade scenarios so stage-storage and pre-breach lake fills work.
    if cascade_cfg is not None:
        wse_m = float(cascade_cfg["reservoir"]["z_crest_m"])
    else:
        dam_height = sc_cfg.get("dam_height_m", 55.0)
        wse_m = thalweg_z + dam_height
        logger.info("M2: thalweg %.0f m + blockage %.0f m -> WSE %.0f m",
                    thalweg_z, dam_height, wse_m)
        # The pool cannot stand above the structure holding it.
        #
        # `thalweg_z` is the DEM-SNAPPED bed, not the configured `thalweg_m`,
        # and the snap moves with resolution. Every non-cascade scenario defines
        # `wse_m == thalweg_m + dam_height_m` exactly, so substituting the
        # snapped bed silently shifts the pool level: measured 2026-09-18, at
        # coarsen 2 phutkal snaps to 3737.3 m against a configured 3736.11, and
        # `thalweg_z + 69.0` lands at 3806.3 m -- 1.19 m ABOVE the sourced
        # 3805.11 m crest. The fill then tops the barrier, reaches the domain
        # edge, is correctly rejected as unconfined, and the run falls back to
        # the typed volume labelled PROXY, which G1 then fails. Clamping it
        # makes the fill confined and holds 27.1 MCM against a configured 30.0.
        #
        # This is the same species as the deleted `wse_m + 5.0`: a DERIVED level
        # allowed to float above a SOURCED one. `crest_elev_m` is gated and
        # carries its own source and classification, so the crest wins.
        if barrier_crest_elev_m is not None and wse_m > barrier_crest_elev_m:
            logger.warning(
                "M2: derived WSE %.2f m exceeds the sourced barrier crest %.2f m "
                "by %.2f m — clamped to the crest. A pool standing above its own "
                "dam has already overtopped, so it was never impounded by it.",
                wse_m, barrier_crest_elev_m, wse_m - barrier_crest_elev_m)
            wse_m = float(barrier_crest_elev_m)

    # Stage B: the fill seed is the validated geometry manifest's own
    # upstream_seed_xy, not a river-vertex search — validate_geometry() has
    # already proven this point sits outside the barrier in a connected
    # component genuinely separate from the downstream seed before failure,
    # which the old heuristic search never checked.
    seed_xy = geometry_result["upstream_seed_xy"]
    logger.info("M2: fill seeded at validated upstream_seed_xy %s", seed_xy)

    # ── The river as a hydraulic pathway ──────────────────────────────────────
    # Measured before this existed: sampled along the OSM reach that passes the
    # breach, on the very DEM the solver integrates, 44.5% of steps RISE going
    # downstream (phutkal, 28 m), 173 m of total adverse rise against 17 m of
    # net fall, and 92% of points needed more than 2 m of ponding to clear the
    # next downstream sill (largest 30.4 m). rishiganga: 37.6% adverse, largest
    # sill 40.4 m. A gorge floor 30-60 m wide does not resolve in a 30 m
    # surface model, so the "river" the solver sees is a staircase of
    # disconnected bowls and the flood fills bowls instead of running
    # downstream. That is the mechanism behind "the flood stops mid-valley".
    #
    # condition_flowline enforces one physical fact — a river bed does not rise
    # downstream — along the MAPPED centreline only, lowering nothing below an
    # elevation the DEM already reports upstream on the same reach, refusing
    # any reach that would need a cut deeper than the cap, and never touching
    # the barrier or the impounded pool (which is why the protect mask is
    # required: the barrier IS an adverse rise on the flowline, and without the
    # mask the filter would breach the dam by construction).
    #
    # It runs HERE, after the geometry gate, after `dem_crest_along_axis` is
    # sampled and after the breach coordinate is snapped, so none of those sees
    # modified terrain — and BEFORE build_stage_storage, so the fill and the
    # solver share one DEM rather than the two different terrains this pipeline
    # used to carry.
    # ── Give the domain an outlet ─────────────────────────────────────────────
    # condition_dem walls every no-data cell at max(DEM)+100 m, which on a
    # reprojected AOI is 94-98% of the boundary ring — measured on phutkal,
    # 4555/4642 boundary cells at 6504 m with the lowest open cell 981 m above
    # the valley floor, and `volume_outflow_m3` identically 0.0 in every run.
    # open_river_outlets punches through that wall only where the MAPPED river
    # crosses out of the data footprint below the release elevation, at the bed
    # level the river already had. See its docstring for the rejected
    # alternative (un-walling the whole wedge invented a bypass around the
    # barrier and merged the upstream pool with the downstream valley).
    from src.m2_geometry.dem_utils import condition_flowline, open_river_outlets
    outlet_report: dict = {"available": False, "reason": "not attempted"}
    try:
        _river_geoms_out = list(rivers.to_crs(crs).geometry) if len(rivers) else []
        dem_elev, outlet_report = open_river_outlets(
            dem_elev, wall_mask, transform, _river_geoms_out,
            source_elev_m=float(thalweg_z),
        )
    except Exception as exc:                                  # noqa: BLE001
        logger.warning("M2: river outlet not opened (%s: %s) — the domain stays "
                       "sealed and the flood can only accumulate",
                       type(exc).__name__, exc)
        outlet_report = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    flowline_report: dict = {"available": False, "reason": "not attempted"}
    channel_mask = None
    try:
        _river_geoms = list(rivers.to_crs(crs).geometry) if len(rivers) else []
        _protect = (np.asarray(geometry_result["barrier_mask"], dtype=bool)
                    | np.asarray(geometry_result["upstream_basin_mask"], dtype=bool))
        dem_elev, channel_mask, flowline_report = condition_flowline(
            dem_elev, transform, _river_geoms, protect_mask=_protect,
        )
    except Exception as exc:                                  # noqa: BLE001
        # Terrain conditioning must never take a run down: without it the model
        # is wrong in a known, reported way, which is better than no model.
        logger.warning("M2: flowline conditioning skipped (%s: %s) — the mapped "
                       "river stays a staircase of bowls and the flood will pond",
                       type(exc).__name__, exc)
        channel_mask = None
        flowline_report = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    # ── P2 Gate 1: DEM-confinement diagnostic, every scenario ──────────────────
    # Report-only. Runs build_stage_storage's connected-pool confinement check
    # for BOTH cascade and non-cascade scenarios (previously only reachable on
    # the non-cascade path, run_pipeline.py's old single call site below). This
    # does NOT feed wse_m, impounded_vol_m3, or vol_prov — reconciling the
    # DEM-derived curve against the cascade path's analytical power law is
    # Gate 2's job. Gate 1 only answers "is this scenario's impoundment
    # confined in this DEM at all," which memory.md already knows the answer
    # to for two scenarios (Annamayya: dam not resolvable in a 30 m DEM;
    # South Lhonak: Chungthang is a different structure at a different
    # elevation than the DEM/AOI's lake) — this makes that status visible for
    # every scenario, not asserted from memory.
    geometry_diagnostic: dict = {"scenario": scenario_key, "cascade": cascade_cfg is not None}
    # Gate 2 (Stage C) output: when the cascade path's reconciliation says the
    # DEM curve agrees with the analytical power law well enough to trust,
    # this is set to the (z_arr, V_arr) pair to feed into
    # simulate_reservoir_cascade below. None otherwise (cascade calls then
    # fall back to their existing analytical-law default).
    stage_storage_curve_for_cascade: tuple[np.ndarray, np.ndarray] | None = None
    # Gate 2 (Stage C) numeric disagreement threshold. Justified in
    # findings_results.md (2026-09-12 entry) against a real measured
    # rishiganga gap of ~6.4e6 % and fill.py's own documented DEM volume
    # uncertainty (~10-20% in gorges): 25% sits just above that documented
    # sampling-noise ceiling, while remaining nine orders of magnitude below
    # the one real structural-disagreement case measured so far — any actual
    # terrain-resolution mismatch overshoots this threshold by a huge margin.
    RECONCILIATION_GAP_THRESHOLD_PCT = 25.0
    try:
        _diag_geom = build_stage_storage(
            dem_path=dem_path, wse_m=wse_m, seed_xy=seed_xy,
            barrier_mask=geometry_result["barrier_mask"],
            barrier_crest_m=barrier_crest_elev_m,
            dem_array=dem_elev, transform=transform,
        )
        geometry_diagnostic.update({
            "confined": True,
            "volume_m3": _diag_geom.volume_m3,
            "area_m2": _diag_geom.area_m2,
            "dam_height_m": _diag_geom.dam_height_m,
            "freeboard_m": _diag_geom.freeboard_m,
            "crest_elev_m": _diag_geom.crest_elev_m,
        })
        logger.info("M2 Gate1: %s DEM-confined pool OK — V=%.3e m^3, H=%.1f m, "
                    "freeboard=%.1f m", scenario_key, _diag_geom.volume_m3,
                    _diag_geom.dam_height_m, _diag_geom.freeboard_m)

        # ── P2 Gate 2 (Stage C): stage-storage reconciliation, cascade scenarios only ──
        # The non-cascade branch below already feeds `_diag_geom`'s real curve
        # (well, its own build_stage_storage call's curve) straight into
        # get_hydrographs — it has no analytical power law to reconcile
        # against, so Gate 2 is meaningless there and is skipped.
        if cascade_cfg is not None:
            from src.m3_breach.cascade import reconcile_stage_storage
            res_cfg = cascade_cfg["reservoir"]
            dem_z_min_m = _diag_geom.wse_m - _diag_geom.dam_height_m
            recon = reconcile_stage_storage(
                res_cfg, dem_z_min_m,
                _diag_geom.stage_curve_h, _diag_geom.stage_curve_V,
                _diag_geom.wse_m,
            )
            if recon["reconciliation_status"] == "FRL_BELOW_DEM_POOL_FLOOR":
                geometry_diagnostic.update({
                    "reconciliation_status": "FRL_BELOW_DEM_POOL_FLOOR",
                    "reconciliation_gap_m3": None,
                    "reconciliation_gap_pct": None,
                    "stage_storage_source": "ANALYTICAL",
                })
                logger.warning(
                    "M2 Gate2: %s FRL (%.1f m) is below the DEM-confined pool's "
                    "floor (%.1f m) at this seed — cannot compare, keeping the "
                    "analytical power law", scenario_key, recon["z_frl_m"], dem_z_min_m)
            else:
                gap_pct = recon["reconciliation_gap_pct"]
                agrees = gap_pct <= RECONCILIATION_GAP_THRESHOLD_PCT
                geometry_diagnostic.update({
                    "reconciliation_status": "AGREE" if agrees else "DISAGREE_SURFACED",
                    "reconciliation_gap_m3": recon["reconciliation_gap_m3"],
                    "reconciliation_gap_pct": gap_pct,
                    "reconciliation_threshold_pct": RECONCILIATION_GAP_THRESHOLD_PCT,
                    "stage_storage_source": "DEM" if agrees else "ANALYTICAL",
                })
                if agrees:
                    dem_z_abs = dem_z_min_m + _diag_geom.stage_curve_h
                    stage_storage_curve_for_cascade = (dem_z_abs, _diag_geom.stage_curve_V)
                    logger.info("M2 Gate2: %s DEM/analytical stage-storage agree "
                                "(gap=%.2f%% <= %.1f%%) — promoting the DEM curve",
                                scenario_key, gap_pct, RECONCILIATION_GAP_THRESHOLD_PCT)
                else:
                    logger.warning("M2 Gate2: %s DEM/analytical stage-storage DISAGREE "
                                   "(gap=%.2f%% > %.1f%%) — keeping the analytical power "
                                   "law, disagreement surfaced in geometry_diagnostic",
                                   scenario_key, gap_pct, RECONCILIATION_GAP_THRESHOLD_PCT)
    except Exception as exc:
        geometry_diagnostic.update({"confined": False, "reason": str(exc)})
        logger.warning("M2 Gate1: %s DEM fill NOT confined (%s) — this scenario's "
                       "impoundment is not resolvable in this DEM; see memory.md "
                       "before assuming a coordinate fix will help", scenario_key, exc)

    # ── M3: breach hydrograph (cascade-aware) ─────────────────────────────────
    # If the scenario declares a `cascade` block, run the physics-based 1D
    # routing engine.  Otherwise fall back to the standard DEM-fill + Froehlich
    # ensemble.  All cascade scenarios (including Annamayya) are now driven
    # uniformly by their cascade config in SCENARIOS.
    if cascade_cfg is not None:
        from src.m3_breach.cascade import simulate_reservoir_cascade
        from src.m3_breach.ensemble import Hydrograph
        from src.m3_breach import BreachParams, FailureMechanism

        # FS-31: this floor used to be a hardcoded 20,000 s applied to every
        # cascade scenario, justified only by Annamayya's Cheyyeru-to-Pennar
        # corridor length (EVD-28) -- a scenario-specific number with no
        # business governing rishiganga or south_lhonak's runs. Each cascade
        # scenario now states its own minimum coverage duration; scenarios
        # that don't declare one get no floor beyond what the caller asked
        # for.
        min_duration_s = float(cascade_cfg.get("min_coverage_duration_s", 0.0))
        total_duration_s = max(total_duration_s, min_duration_s)

        # Which FailureMechanism drives this cascade's trigger/breach-growth
        # dispatch. No cascade scenario in data_fetcher.py's SCENARIOS carries
        # a failure_mechanism key today (confirmed, FS-56) — deciding which
        # mechanism each real scenario (Annamayya's Pincha ring bund, South
        # Lhonak's moraine vs. Chungthang, etc.) actually matches is a
        # scenario-authoring decision deliberately deferred as follow-up work
        # (see Stage D+E report), not guessed here. Default to
        # OVERTOPPING_EROSION (today's actual behavior for every cascade
        # scenario), but honor an explicit override if a future scenario
        # config supplies one.
        _mech_str = cascade_cfg.get("failure_mechanism")
        cascade_mechanism = (
            failure_mode_to_mechanism(_mech_str) if _mech_str is not None
            else FailureMechanism.OVERTOPPING_EROSION
        )
        require_implemented_mechanism(cascade_mechanism)

        # All cascade scenarios use the generic path.
        _up = cascade_cfg.get("upstream", {})
        _label = (
            f"{_up.get('classification','?')} upstream surge -> "
            f"Muskingum 1D -> {scenario_key} reservoir -> Froehlich ensemble"
        )
        _report("breach", 0.0, _label)
        res_cent = simulate_reservoir_cascade(cascade_cfg, total_duration_s=total_duration_s, dt_s=30.0, breach_tier="central", stage_storage_curve=stage_storage_curve_for_cascade, failure_mechanism=cascade_mechanism)
        res_pess = simulate_reservoir_cascade(cascade_cfg, total_duration_s=total_duration_s, dt_s=30.0, breach_tier="pessimistic", stage_storage_curve=stage_storage_curve_for_cascade, failure_mechanism=cascade_mechanism)
        res_opti = simulate_reservoir_cascade(cascade_cfg, total_duration_s=total_duration_s, dt_s=30.0, breach_tier="optimistic", stage_storage_curve=stage_storage_curve_for_cascade, failure_mechanism=cascade_mechanism)
        _pess_label = (
            "Froehlich Upper / 336m Structural Envelope"
            if scenario_key == "annamayya"
            else "Froehlich (2008) upper envelope"
        )

        def _make_hg(res, method_label, arm_name):
            # The continuous integrator's t_s runs from -pre_breach_s onward;
            # downstream consumers (2D solver, hydrograph.json) expect the
            # historical 0-based, non-negative "T=0 = event anchor" convention.
            mask = res.t_s >= 0.0
            return Hydrograph(
                arm=arm_name,
                t_s=res.t_s[mask], Q_m3s=res.q_breach_m3s[mask],
                params=BreachParams(
                    method=method_label,
                    breach_width_m=res.ensemble_metadata["breach_width_m"],
                    side_slope_hv=1.0,
                    formation_time_h=res.ensemble_metadata["formation_time_min"] / 60.0,
                    peak_discharge_m3s=res.ensemble_metadata["peak_breach_q_m3s"],
                ),
            )

        cent = _make_hg(res_cent, "Froehlich (2008) Cascade", "central")
        pess = _make_hg(res_pess, _pess_label, "pessimistic")
        opti = _make_hg(res_opti, "Froehlich (2008) Lower Bound", "optimistic")

        # The cascade path routes real upstream/reservoir config through 1D
        # Muskingum physics rather than filling a DEM or reading a flat
        # configured constant — so its volume is computed/live, same as the
        # DEM stage-storage path below for non-cascade scenarios.
        vol_prov = Provenance.COMPUTED_LIVE

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
            "test_11_volume_plausibility_passed": res_cent.v_breach_total_m3 <= (res_cent.v_initial_m3 + res_cent.v_inflow_total_m3),
            "breach_geometry_type":            "MODEL RECONSTRUCTION",
            "breach_geometry_source":          "empirical breach formula (Froehlich 2008)",
        }
        if scenario_key == "annamayya":
            cascade_audit["reference_structural_upper_bound_m"] = 336.0
            # Record agency-sourced ensemble arms (EVD-15: MHA/CWC/IISc)
            for tier_key in ("optimistic", "central", "pessimistic"):
                ens_arm = cascade_cfg.get("breach_ensemble", {}).get(tier_key, {})
                if "agency" in ens_arm:
                    cascade_audit[f"{tier_key}_agency"] = ens_arm["agency"]
                    cascade_audit[f"{tier_key}_agency_peak_inflow_m3s"] = ens_arm.get("agency_peak_inflow_m3s")

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

        # Impounded volume from the DEM rather than a configured constant.
        stage_h = stage_V = None

        try:
            geom = build_stage_storage(
                dem_path=dem_path, wse_m=wse_m, seed_xy=seed_xy,
                barrier_mask=geometry_result["barrier_mask"],
                barrier_crest_m=barrier_crest_elev_m,
                dem_array=dem_elev, transform=transform,
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
        # failure_mode arrives here as the API/CLI string; convert to the
        # FailureMechanism enum and fail clearly (not a silently-substituted
        # generic breach) if it maps to a NOT_IMPLEMENTED mechanism — see
        # FLOODSIGHT_DEEP_REVIEW_2026-09-11.md's mechanism status table, §P
        # acceptance condition #3.
        mechanism = failure_mode_to_mechanism(failure_mode)
        require_implemented_mechanism(mechanism)
        # `dam_height_m` is the STRUCTURAL height, which MacDonald uses to size
        # the breach prism (`Hd` -> `w_mean` -> `B_avg`). It used to be set to
        # `dam_height * 1.15` -- an uncited multiplier with no provenance label,
        # sitting in the production breach path and changing every reported
        # breach width (audit Part VI N-2). §41 bans exactly that shape, so it is
        # gone rather than documented.
        #
        # Structural height now equals water height. That is a stated
        # simplification -- a real embankment has freeboard above its pool -- and
        # it is the honest one available: no scenario carries a sourced
        # structural height. The upgrade path is a `structural_height_m` field on
        # the geometry manifest, sourced and gated the way `crest_elev_m` already
        # is; until one exists, inventing 15 % of it is fabrication.
        dam = DamGeometry(
            height_m=dam_height, volume_m3=impounded_vol_m3,
            dam_height_m=dam_height, failure_mechanism=mechanism,
            crest_length_m=crest_length_m,
            dam_type=dam_type,
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

    # ── Channel roughness, from the mapped river ──────────────────────────────
    # Applied LAST so it wins over both the elevation banding and the GHS-POP
    # urban override: those are land-cover proxies and neither of them knows
    # where the channel is. In Derna the population override was setting the
    # wadi bed itself to the built-up n = 0.08.
    #
    # Measured on the idealised channel (E7): channel n 0.020 -> 0.100 moved the
    # front 4.26 km -> 1.70 km and peak speed 6.52 -> 2.00 m/s, while floodplain
    # n 0.035 -> 0.150 moved the front not at all (3.12 km throughout) because
    # the flow stays in the channel. Differentiating the two is the lever that
    # exists; an undifferentiated grid is not.
    from src.m4_solvers.roughness import apply_channel_roughness, CHANNEL_MANNING_N
    channel_roughness_report: dict = {"available": False, "reason": "not attempted"}
    try:
        _river_geoms_n = list(rivers.to_crs(crs).geometry) if len(rivers) else []
        manning_grid, channel_roughness_report = apply_channel_roughness(
            manning_grid, _river_geoms_n, transform,
            n_channel=float(sc_cfg.get("channel_manning_n", CHANNEL_MANNING_N)),
            channel_mask=channel_mask,
        )
    except Exception as exc:                                  # noqa: BLE001
        logger.warning("M4: channel roughness skipped (%s: %s) — the channel "
                       "carries whatever the land-cover proxy assigned it",
                       type(exc).__name__, exc)
        channel_roughness_report = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    # ── Stage F Part 1: real t=0 initial condition for the 2D solver ──────────
    # Defect C1: every flood used to start on a dry bed. `initial_depth`
    # already works in run_2d_swe_simulation (defaults to dry) -- it was
    # simply never passed. The reservoir exactly as it stood the instant
    # before breach initiation is the physically correct t=0 state, computed
    # over the SAME dem_elev grid the solver runs on (confirmed: no separate
    # "reservoir domain" exists in this codebase's architecture).
    from src.m2_geometry.fill import compute_lake_depth_grids as _lake_grids_for_ic
    initial_depth = None
    initial_condition_source = None
    try:
        if cascade_cfg is not None:
            # res_cent already carries the full continuous trajectory
            # (pre-trigger through post-trigger) -- reuse it rather than
            # running simulate_reservoir_cascade a second time. t_s <= 0.0 is
            # the same "pre-trigger" convention the pre-breach lake-frame
            # block below uses; its last sample is the fullest pool right
            # before the event anchor.
            _pb_idx = np.flatnonzero(res_cent.t_s <= 0.0)
            _ic_level = float(res_cent.reservoir_elevation_m[_pb_idx[-1]]) if _pb_idx.size \
                else float(res_cent.reservoir_elevation_m[0])
            _ic_frames = _lake_grids_for_ic(
                dem_array=dem_elev, transform=transform, seed_xy=seed_xy,
                wse_m=wse_m, barrier_mask=geometry_result["barrier_mask"],
                barrier_crest_m=barrier_crest_elev_m, levels_m=[_ic_level],
            )
            if _ic_frames:
                initial_depth = _ic_frames[0]["depth_grid"]
                initial_condition_source = "ROUTED_PREBREACH_LAKE"
        else:
            # No routed pre-breach trajectory exists for non-cascade
            # scenarios -- only the ASSUMED_FRACTION frames already used for
            # the lake-formation animation. Reuse `reservoir_fill` (already
            # threaded through execute_full_simulation and used above to
            # scale impounded_vol_m3) as the fraction, rather than
            # introducing a second, disconnected "how full is the reservoir"
            # number: the reservoir is nearly full right before an
            # assumed-fraction breach, and this keeps that one assumption
            # consistent across the volume and the initial water surface.
            #
            # `reservoir_fill` is a VOLUME fraction. `impounded_vol_m3` above is
            # `geom.volume_m3 * reservoir_fill`, and "the reservoir is 90 % full"
            # means 90 % of its capacity everywhere else in this project.
            # `compute_lake_depth_grids(fractions=...)` reads its fractions as
            # STAGE instead -- `z_min + f*(wse - z_min)` -- so passing it
            # straight through made the comment above FALSE: the two were not
            # consistent, they were 10.6 % apart.
            #
            # Measured on phutkal at 55.8 m cells, 2026-09-18: a 0.9 STAGE fill
            # holds 22.044 MCM = 0.8044 of the pool, against a 0.9 VOLUME fill's
            # 24.666 MCM. G1 compares exactly these two numbers and FAILED at
            # ratio 0.8937 (tolerance 0.95-1.05), accusing the run of water that
            # "did not come from the impoundment" -- when it had, and only the
            # meaning of 0.9 differed. Converted to a level first.
            _ic_level_vol = level_for_volume_fraction(
                dem_array=dem_elev, transform=transform, seed_xy=seed_xy,
                wse_m=wse_m, fraction=reservoir_fill,
                barrier_mask=geometry_result["barrier_mask"],
                barrier_crest_m=barrier_crest_elev_m,
            )
            logger.info("M4: reservoir_fill %.3f read as a VOLUME fraction "
                        "-> initial level %.2f m (crest %.2f m)",
                        reservoir_fill, _ic_level_vol, wse_m)
            _ic_frames = _lake_grids_for_ic(
                dem_array=dem_elev, transform=transform, seed_xy=seed_xy,
                wse_m=wse_m, barrier_mask=geometry_result["barrier_mask"],
                barrier_crest_m=barrier_crest_elev_m, levels_m=[_ic_level_vol],
            )
            if _ic_frames:
                initial_depth = _ic_frames[0]["depth_grid"]
                initial_condition_source = "ASSUMED_FRACTION_LAKE"
        if initial_depth is not None:
            logger.info("M4: initial_depth wired from %s (wet cells=%d, "
                        "volume=%.3e m^3)", initial_condition_source,
                        int((initial_depth > 0.0).sum()),
                        float(initial_depth.sum() * dx_m * dy_m))
    except Exception as exc:                                  # noqa: BLE001
        logger.warning("M4: initial_depth unavailable (%s) — solver starts "
                       "on a dry bed", exc)
        initial_depth = None
        initial_condition_source = None

    # ── Stage F Part 2: momentum for the injected inflow ───────────────────────
    # Defect M6: injected mass used to carry no momentum. v_jet = Q / (width *
    # head) at each timestep (continuity through the breach opening),
    # directed along the breach-to-downstream axis. A single fixed direction
    # for the whole run: the breach axis does not rotate during one event.
    downstream_xy = geometry_result.get("downstream_seed_xy")
    inflow_direction = None
    if downstream_xy is not None:
        _rows, _cols = rtransform.rowcol(transform, [bx, downstream_xy[0]], [by, downstream_xy[1]])
        _dir_iy = float(_rows[1] - _rows[0])   # downstream_iy - breach_iy
        _dir_ix = float(_cols[1] - _cols[0])   # downstream_ix - breach_ix
        _dir_norm = float(np.hypot(_dir_ix, _dir_iy))
        if _dir_norm > 1e-9:
            inflow_direction = (_dir_ix / _dir_norm, _dir_iy / _dir_norm)

    _WIDTH_HEAD_EPS = 1.0  # metres; below this, treat v_jet as undefined (0.0)

    def _hydrograph_v_ms(hg, cascade_result=None) -> np.ndarray | None:
        """Jet velocity at each hg.t_s sample: v = Q / (width * head).

        Cascade path: width/head/invert come from `cascade_result`'s real
        per-timestep arrays (breach_width_m, reservoir_elevation_m,
        breach_invert_m), interpolated onto hg.t_s (which uses the same
        t_s axis, just clipped to t_s >= 0). Non-cascade path: width comes
        from the same shared breach_kernel.breach_width_at growth law
        get_hydrographs already used; head has no routed trajectory outside
        `m3_breach` to read (see findings_results.md, 2026-09-12 Stage F
        entry) and falls back to the constant `dam.height_m` -- a rougher
        approximation than the cascade path's, flagged there rather than
        guessed silently.
        """
        if inflow_direction is None:
            return None
        if cascade_result is not None:
            width = np.interp(hg.t_s, cascade_result.t_s, cascade_result.breach_width_m)
            invert = np.interp(hg.t_s, cascade_result.t_s, cascade_result.breach_invert_m)
            head = np.interp(hg.t_s, cascade_result.t_s, cascade_result.reservoir_elevation_m) - invert
        else:
            from src.m3_breach.breach_kernel import breach_width_at
            t_f = max(float(hg.params.formation_time_h) * 3600.0, 1.0)
            width = np.array([breach_width_at(t, t_f, hg.params.breach_width_m) for t in hg.t_s])
            # route_breach now returns the head on the invert it already
            # computed every step to evaluate the weir. It used to be thrown
            # away, and this fell back to a CONSTANT dam.height_m — a head that
            # never dropped as the reservoir emptied, so the injected momentum
            # stayed at its full-reservoir value through the entire recession.
            if getattr(hg, "head_m", None) is not None and len(hg.head_m) == len(hg.t_s):
                head = np.asarray(hg.head_m, dtype=float)
            else:
                head = np.full_like(hg.t_s, float(dam.height_m))
        denom = width * head
        return np.where(denom > _WIDTH_HEAD_EPS, hg.Q_m3s / np.maximum(denom, _WIDTH_HEAD_EPS), 0.0)

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

    # Which cascade trajectory backs each arm's jet-velocity estimate (None on
    # the non-cascade path, where there is only one set of breach-geometry
    # arrays regardless of arm).
    _cascade_result_by_arm = (
        {"central": res_cent, "pessimistic": res_pess, "optimistic": res_opti}
        if cascade_cfg is not None else {}
    )

    # ── P3 (audit SS38, defects C2 + C1): cut the opening, delete the injection ─
    # `breach_mask` and `upstream_basin_mask` were both computed by
    # `validate_geometry` and both thrown away. They are what an opening needs:
    # the mask says WHERE the structure fails, and the basin is the control
    # volume the discharge is measured across.
    #
    # What this replaces: `snap_to_thalweg` put the release at the lowest cell
    # within 300 m, which is always the pool floor -- so the breach hydrograph
    # was injected as a 5x5 Gaussian source INTO the reservoir, 545 m inside it,
    # at a cell carrying 58.0 m of initial-condition water. That is why the
    # domain held 2.00x the impounded volume and why |v| reached 330 m/s.
    from src.m4_solvers.swe_2d import BreachOpening

    breach_opening = None
    breach_opening_by_arm: dict = {}
    upstream_cv_mask = None
    opening_report: dict = {"active": False}
    try:
        _open_mask = geometry_result["breach_mask"] & geometry_result["barrier_mask"]
        if not _open_mask.any():
            raise ValueError(
                "breach_zone and the barrier do not overlap at this resolution, "
                "so there is no cell the structure can fail in")
        # Width grows ALONG the dam axis, so distance is measured along it, not
        # radially. Without an axis "breach width" has no direction (FS-14).
        _axis_geom = geometry_result["projected_geometry"].get("dam_axis")
        if _axis_geom:
            from shapely.geometry import Point as ShapelyPoint, shape as _shape
            _axis = _shape(_axis_geom[0])
            _rows_i, _cols_i = np.indices(dem_elev.shape)
            _xs, _ys = rtransform.xy(transform, _rows_i.ravel(), _cols_i.ravel())
            _bx_a, _by_a = _axis.interpolate(_axis.project(
                ShapelyPoint(bx, by))).coords[0]
            _axis_d = np.hypot(np.asarray(_xs) - _bx_a,
                               np.asarray(_ys) - _by_a).reshape(dem_elev.shape)
        else:
            raise ValueError("no dam_axis role: breach width has no direction")

        # One opening PER ARM (audit Part V RC-5 / E-4). Everything except the
        # breach geometry is shared -- same structure, same axis, same natural
        # bed -- so the arms differ only in how wide the breach grows and how
        # fast, which is exactly what the pessimistic/central/optimistic
        # ensemble is a spread over.
        #
        # Before this, only the central arm got an opening. The side arms were
        # dispatched with `hydrograph_Q_m3s` AND `initial_depth` and no opening,
        # so each of them re-supplied the impoundment on top of the pool already
        # in the domain -- defect C1, measured at exactly 2.00x, surviving in
        # `envelope.tif` and `envelope.geojson` long after the central arm was
        # fixed. The validity gates read the central arm only, so G1 never saw
        # it.
        _invert_final = float(np.nanmin(dem_natural[_open_mask]))
        for _arm_name, _arm_hg in arms_to_run:
            _bp = _arm_hg.params
            breach_opening_by_arm[_arm_name] = BreachOpening(
                mask=_open_mask,
                z_natural=dem_natural,
                crest_elev_m=barrier_crest_elev_m,
                # The breach erodes to the natural bed under the structure,
                # which is the deepest a breach can physically cut.
                invert_final_m=_invert_final,
                formation_s=float(_bp.formation_time_h) * 3600.0,
                final_width_m=float(_bp.breach_width_m),
                trigger_s=0.0,
                axis_distance_m=_axis_d,
            )
        breach_opening = breach_opening_by_arm["central"]

        # A breach narrower than the grid never opens, and does so SILENTLY.
        # `run_2d_swe_simulation` applies width(t) as
        # `mask & (axis_distance <= width/2)`, so if no cell centre falls inside
        # half the FINAL width, the opening never cuts, the pool never drains,
        # and Q is measured as identically zero -- while the report still says
        # `active: true`. Measured on phutkal at coarsen 4 (2026-09-13): a
        # 37.7 m breach against 111.6 m cells opened 0 cells and produced a
        # measured peak of 0 m^3/s with no warning anywhere. At coarsen 1 the
        # same breach opens 10 cells.
        #
        # This refuses instead. Widening the breach to suit the mesh would be
        # fabricating the structure (audit §41); the answer is a finer grid or
        # a sourced width the grid can carry.
        _will_open = {
            nm: int((_open_mask & (_axis_d <= 0.5 * op.final_width_m)).sum())
            for nm, op in breach_opening_by_arm.items()
        }
        # ...and they must be FACE-connected to the water. Water crosses cell
        # faces, never corners -- the same 4-connectivity fact
        # `escape_head_4connected` and `_orthogonalise` already exist for. An
        # opened cell touching the pool only at a corner carries no flux, so the
        # breach cuts, the pool sits beside it, and the run reports a discharge
        # of exactly zero.
        #
        # Measured on phutkal at coarsen 1 (2026-09-13): the central arm's
        # 37.7 m breach opened 2 cells, both with ZERO face-adjacent wet
        # neighbours -- nearest wet pool cell 1.4 cells away, i.e. diagonal.
        # Froehlich's 98.6 m breach opens 5 cells, 1 face-adjacent; von Thun's
        # 199.9 m opens 10, with 3.
        _pool_now = (initial_depth > 0.0) if initial_depth is not None else np.zeros_like(dem_elev, bool)
        def _face_touching(_op):
            nb = np.zeros_like(_op)
            nb[1:, :] |= _op[:-1, :]; nb[:-1, :] |= _op[1:, :]
            nb[:, 1:] |= _op[:, :-1]; nb[:, :-1] |= _op[:, 1:]
            return int((nb & _pool_now & ~_op).sum())
        _central_open = _open_mask & (_axis_d <= 0.5 * breach_opening.final_width_m)
        _touch = _face_touching(_central_open)
        if _will_open.get("central", 0) > 0 and _touch == 0:
            raise ValueError(
                f"the breach opens {_will_open['central']} cell(s) but none of "
                f"them shares a FACE with the impounded water — the nearest wet "
                f"cell is diagonal, and a corner carries no flux. The opening "
                f"would cut and the run would report a discharge of exactly "
                f"zero. This is a breach-placement/resolution problem: the "
                f"breach axis point and the pool do not meet on this grid. Do "
                f"NOT widen the breach to reach the water")
        if _will_open.get("central", 0) == 0:
            _bw = breach_opening_by_arm["central"].final_width_m
            raise ValueError(
                f"the breach is narrower than the grid: B_final = {_bw:.1f} m "
                f"against {dx_m:.1f} m cells, so no cell centre lies within "
                f"B/2 = {0.5 * _bw:.1f} m of the breach axis and the opening "
                f"would never cut — the run would report a live opening and a "
                f"discharge of exactly zero. Run at a finer coarsening, or "
                f"source a breach width this grid can resolve; do NOT widen the "
                f"breach to suit the mesh")
        upstream_cv_mask = (geometry_result["upstream_basin_mask"]
                            | geometry_result["barrier_mask"])
        opening_report = {
            "active": True,
            "cells": int(_open_mask.sum()),
            "crest_elev_m": barrier_crest_elev_m,
            "invert_final_m": breach_opening.invert_final_m,
            "formation_s": breach_opening.formation_s,
            "final_width_m": breach_opening.final_width_m,
            "arms_with_openings": sorted(breach_opening_by_arm),
            "cells_opening_at_full_width": _will_open,
            "final_width_m_by_arm": {k: v.final_width_m
                                     for k, v in breach_opening_by_arm.items()},
            "formation_s_by_arm": {k: v.formation_s
                                   for k, v in breach_opening_by_arm.items()},
            "control_volume_cells": int(upstream_cv_mask.sum()),
            "routed_peak_q_m3s": float(np.max(cent.Q_m3s)) if cent.Q_m3s.size else None,
        }
        logger.info("M4: breach OPENING over %d cells, crest %.2f -> invert %.2f m, "
                    "B=%.1f m over t_f=%.0f s. The injection is deleted; Q is now "
                    "measured across %d control-volume cells.",
                    opening_report["cells"], barrier_crest_elev_m,
                    breach_opening.invert_final_m, breach_opening.final_width_m,
                    breach_opening.formation_s, opening_report["control_volume_cells"])
    except Exception as exc:                                  # noqa: BLE001
        # Fails LOUD in the validity block rather than silently reverting to the
        # injection -- reinstating the injection here would restore the 2.00x
        # double count the whole of P3 exists to remove (audit SS41).
        breach_opening = None
        breach_opening_by_arm = {}
        upstream_cv_mask = None
        opening_report = {"active": False, "reason": str(exc)}
        logger.error("M4: no breach opening could be built (%s) — the run will "
                     "report zero release rather than inject one", exc)


    _payload = lambda name, hg: {                       # noqa: E731
        "elev": dem_elev, "dx": dx_m, "dy": dy_m,
        "ix": breach_ix, "iy": breach_iy,
        "t_s": hg.t_s,
        # Zeroed when this arm has an opening: the impoundment is already in the
        # domain as `initial_depth` and the opening drains it. Supplying both is
        # defect C1 and `run_2d_swe_simulation` refuses it outright.
        "Q": (np.zeros_like(hg.Q_m3s) if breach_opening_by_arm.get(name) is not None
              else hg.Q_m3s),
        "dur": total_duration_s, "manning": manning_grid,
        "name": f"{scenario_key}_{name}",
        "initial_depth": initial_depth,
        "v_ms": (None if breach_opening_by_arm.get(name) is not None
                 else _hydrograph_v_ms(hg, _cascade_result_by_arm.get(name))),
        "direction": (None if breach_opening_by_arm.get(name) is not None
                      else inflow_direction),
        "breach_opening": breach_opening_by_arm.get(name),
        "upstream_cv_mask": upstream_cv_mask,
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

    # Central always contributes: it runs directly, not in a try/except, so
    # the pipeline would already have failed before this point if it raised.
    arms_contributing = ["central"]

    # With an opening, Q is an OUTPUT. The hydrograph the ensemble routed is
    # still computed and still written, but as the 0-D CHECK it now is, never as
    # the driver (audit SS38.4).
    _central_Q = (np.zeros_like(cent.Q_m3s) if breach_opening is not None
                  else cent.Q_m3s)

    logger.info("M4: Routing central breach arm...")
    sim_results["central"] = run_2d_swe_simulation(
        elevation_grid=dem_elev, dx_m=dx_m, dy_m=dy_m,
        inflow_x_idx=breach_ix, inflow_y_idx=breach_iy,
        hydrograph_t_s=cent.t_s, hydrograph_Q_m3s=_central_Q,
        total_duration_s=total_duration_s, save_interval_s=save_interval_s,
        manning_n=manning_grid, scenario_name=f"{scenario_key}_central",
        progress_cb=_central_progress,
        initial_depth=initial_depth,
        hydrograph_v_ms=(None if breach_opening is not None
                         else _hydrograph_v_ms(cent, _cascade_result_by_arm.get("central"))),
        inflow_direction=(None if breach_opening is not None else inflow_direction),
        breach_opening=breach_opening,
        upstream_cv_mask=upstream_cv_mask,
        # `total_duration_s` is a safety CAP, not the target. The flood decides
        # when it is over; a fixed window either truncates a flood still on the
        # move or integrates still water for nothing. Everything downstream --
        # the timeline, the animation, arrival times -- reads the run's actual
        # `end_time_s`, so the bar spans the flood rather than a constant.
        stop_when_quiescent=True,
    )
    _c = sim_results["central"]
    logger.info(
        "M4: run ended at t=%.0f s (%.1f min) %s (cap was %.0f s)",
        _c.end_time_s, _c.end_time_s / 60.0,
        "- flood quiescent" if _c.stopped_early else "- hit the cap, flood may still "
        "have been moving", total_duration_s)
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
            arms_contributing.append(arm_name)
        except Exception as exc:                            # noqa: BLE001
            # An envelope arm is uncertainty decoration. Losing one must not
            # lose the run; it is reported so the band is known to be partial.
            logger.warning("M4: %s arm failed (%s) — envelope is built from "
                           "fewer arms and understates the spread",
                           arm_name, exc)
    if pool is not None:
        pool.shutdown(wait=True)

    sim_res = sim_results["central"]

    # ── The MEASURED breach discharge, written beside the routed arms ─────────
    # Until this existed, `res.breach_q_m3s` was computed at every solver step
    # and thrown away: the opening made Q an OUTPUT (audit §38.4), but the only
    # hydrograph any consumer could reach -- `hydrograph.json`, the API, the
    # frontend chart -- was still the 0-D weir curve the opening replaced. A
    # measurement with no path to an output is a defect, not a feature (audit
    # Part VI N-1).
    #
    # Both curves are kept. The routed one is the INDEPENDENT CHECK on the
    # measurement and must NOT be tuned to agree with it -- the disagreement is
    # the finding, and its sign (measured below the weir prediction) is
    # physically expected: the weir formula assumes a full free overfall with a
    # static reservoir head, no approach loss and no lateral contraction
    # (audit §51).
    if sim_res.breach_q_m3s:
        _hg_path = out_dir / "hydrograph.json"
        try:
            _hg = json.loads(_hg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _hg = {}
        _hg["measured"] = {
            "t_s":      [float(x) for x in sim_res.breach_q_t_s],
            "Q_m3s":    [float(x) for x in sim_res.breach_q_m3s],
            "invert_m": [float(x) for x in sim_res.breach_invert_m],
            "width_m":  [float(x) for x in sim_res.breach_width_m],
            "source": ("control-volume storage derivative across "
                       "upstream_basin_mask | barrier_mask, corrected for bed "
                       "lowering; step 0 suppressed as a startup artefact"),
            "note": ("MEASURED, not prescribed. The first samples inherit the "
                     "scheme's startup transient over sub-second timesteps; "
                     "only the integral is defensible instantaneously."),
        }
        with open(_hg_path, "w", encoding="utf-8") as f:
            json.dump(_hg, f, indent=2)
        logger.info("M4: measured breach hydrograph exported — %d samples, "
                    "peak %.0f m^3/s", len(sim_res.breach_q_m3s),
                    max(sim_res.breach_q_m3s))
    else:
        logger.info("M4: no measured breach hydrograph (no opening on this arm)")

    # Max-depth raster (central arm)
    max_depth_tif = _write_depth_raster(sim_res.max_depth_grid,
                                        out_dir / "max_depth.tif", crs, transform)
    logger.info("M4: max depth raster (central) -> %s", max_depth_tif)

    # ── P4 (audit SS39): consequence is flood depth, not total water depth ─────
    # `max_depth.tif` includes the reservoir, and it should -- the map is meant
    # to show the pool. But exposure, arrival and any extent skill score read
    # from it, so the impoundment was being counted as flood. Measured on the
    # certified archive run: 67 of 402 max-depth cells (17 %) were the
    # reservoir, two villages reported max_depth_m = 58.0 (exactly the
    # reservoir depth), and the arrival raster marked the whole pool reached at
    # t = 0. `flood_depth.tif` is `max(max_depth - initial_depth, 0)` and is
    # what every consequence stage samples from here on.
    _init_depth_grid = sim_res.initial_depth_grid
    if _init_depth_grid is None:
        _init_depth_grid = np.zeros_like(sim_res.max_depth_grid)
    flood_depth_grid = np.maximum(sim_res.max_depth_grid - _init_depth_grid, 0.0)
    flood_depth_tif = _write_depth_raster(flood_depth_grid,
                                          out_dir / "flood_depth.tif", crs, transform)
    _pool_cells = int((_init_depth_grid > 0.0).sum())
    _wet_cells = int((sim_res.max_depth_grid > THRESH_CAR).sum())
    reservoir_exclusion = {
        "initial_pool_cells": _pool_cells,
        "max_depth_cells_over_threshold": _wet_cells,
        "flood_depth_cells_over_threshold": int((flood_depth_grid > THRESH_CAR).sum()),
        "initial_pool_volume_m3": float(_init_depth_grid.sum() * dx_m * dy_m),
    }
    logger.info("M4: flood depth raster (reservoir removed) -> %s  "
                "(%d cells over threshold, against %d including the pool)",
                flood_depth_tif, reservoir_exclusion["flood_depth_cells_over_threshold"],
                _wet_cells)

    # Envelope raster (max across all arms)
    envelope_tif = _write_depth_raster(envelope_grid,
                                       out_dir / "envelope.tif", crs, transform)
    
    # Vectorize envelope for frontend display
    envelope_geojson_path = out_dir / "envelope.geojson"
    envelope_fc = _depth_to_geojson(envelope_grid, transform, 0.0, to_wgs84)
    envelope_fc["arms_contributing"] = arms_contributing
    envelope_fc["n_arms_contributing"] = len(arms_contributing)
    with open(envelope_geojson_path, "w", encoding="utf-8") as f:
        json.dump(envelope_fc, f)
    
    logger.info("M4: ensemble extent envelope -> %s (and .geojson)", envelope_tif)

    # ── M4 SPH Scenario Comparison (SIH26161 deliverable i) ───────────────────
    # Run honest 1D SWE-SPH along the scenario thalweg corridor and compare with 2D FV
    try:
        from src.m4_solvers.sph_swe import run_scenario_thalweg_sph
        thalweg_pts = sc_cfg.get("breach_centerline_utm")
        if not thalweg_pts and len(river_pts) >= 2:
            dists = np.hypot(river_pts[:, 0] - bx, river_pts[:, 1] - by)
            downstream_mask = dists < 35000.0
            if downstream_mask.any():
                sorted_idx = np.argsort(dists[downstream_mask])
                thalweg_pts = river_pts[downstream_mask][sorted_idx]
        if thalweg_pts is not None and len(thalweg_pts) >= 2:
            sph_comp = run_scenario_thalweg_sph(
                scenario_key=scenario_key,
                dem_array=dem_elev,
                transform=transform,
                thalweg_pts_utm=thalweg_pts,
                hydrograph_t_s=cent.t_s,
                hydrograph_q_m3s=cent.Q_m3s,
                manning_n=0.035,
                channel_width_m=sc_cfg.get("breach_width_m", 120.0),
                total_duration_s=min(total_duration_s, 3600.0),
                max_depth_array=sim_res.max_depth_grid,
            )
            with open(out_dir / "solver_comparison.json", "w", encoding="utf-8") as f:
                json.dump(sph_comp, f, indent=2)
            logger.info("M4: SPH vs 2D FV thalweg comparison -> %s (RMSE = %.2f m)",
                        out_dir / "solver_comparison.json", sph_comp.get("rmse_m", 0.0))
    except Exception as exc:
        logger.warning("M4: SPH scenario comparison skipped (%s)", exc)

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
    # 1a/1e: For cascade scenarios, use simulate_prebreach_rise to compute
    # physically correct reservoir elevations; for non-cascade, use fraction-
    # based filling as before.
    from src.m2_geometry.fill import (compute_lake_depth_grids,
                                      lake_formation_levels)
    lake_frames = []
    lake_meta = {"scenario": scenario_key, "stages": []}
    try:
        if cascade_cfg is not None:
            # 1e: Cascade path — compute pre-breach elevations from mass continuity.
            # Same continuous integrator as the main hydrograph call site; only
            # the t_s <= 0 (pre-trigger) portion is used for lake-frame sampling.
            # Running it again here (rather than threading through one shared
            # result) is wasteful but out of scope for this task.
            from src.m3_breach.cascade import simulate_reservoir_cascade
            _pb_result = simulate_reservoir_cascade(cascade_cfg)
            _pb_mask = _pb_result.t_s <= 0.0
            pb_t_s = _pb_result.t_s[_pb_mask]
            pb_elev = _pb_result.reservoir_elevation_m[_pb_mask]

            # Subsample to a handful of frames (every ~10-15 sim minutes).
            n_pb = len(pb_t_s)
            step_interval = max(1, int(600.0 / 30.0))  # ~10 min per frame
            sample_indices = list(range(0, n_pb, step_interval))
            if (n_pb - 1) not in sample_indices:
                sample_indices.append(n_pb - 1)
            # Keep only ~6-8 frames to avoid excessive snapshots
            if len(sample_indices) > 8:
                step2 = max(1, len(sample_indices) // 7)
                sample_indices = sample_indices[::step2]
                if (n_pb - 1) not in sample_indices:
                    sample_indices.append(n_pb - 1)

            pb_levels = [float(pb_elev[i]) for i in sample_indices]
            pb_times_min = [float(pb_t_s[i]) / 60.0 for i in sample_indices]
            level_source = "ROUTED"

            lake_depths = compute_lake_depth_grids(
                dem_array=dem_elev,
                transform=transform,
                seed_xy=seed_xy,
                wse_m=wse_m,
                barrier_mask=geometry_result["barrier_mask"],
                barrier_crest_m=barrier_crest_elev_m,
                levels_m=pb_levels,
            )
            lake_dt_mins = pb_times_min
        else:
            # Equal-VOLUME frames over the scenario's SOURCED formation time.
            #
            # This branch used to be four STAGE fractions (0.25/0.50/0.75/0.95)
            # on four hardcoded timestamps. Both were wrong, and the API served
            # the result: measured on phutkal, those four frames held 0.49,
            # 2.16, 3.86 and 11.77 MCM of a ~27 MCM pool, so the frame labelled
            # "25% Capacity" was 1.8 % of it and the first two rendered as
            # nothing. In a gorge 29 % of the height range holds 12.9 % of the
            # water at the bottom and 47 % at the top, so equal stage steps can
            # never give an even-looking animation.
            #
            # `lake_formation_levels` lives in fill.py precisely so this and
            # `scripts/make_lake_formation.py` cannot disagree again -- the
            # script had the fix for a day while the pipeline kept the defect.
            _n_lake_frames = 24
            _lake_levels, _lake_sills, _lake_zmin = lake_formation_levels(
                dem_array=dem_elev,
                transform=transform,
                seed_xy=seed_xy,
                wse_m=wse_m,
                n_frames=_n_lake_frames,
                barrier_mask=geometry_result["barrier_mask"],
                barrier_crest_m=barrier_crest_elev_m,
            )
            level_source = "EQUAL_VOLUME_DEM_FILL"
            lake_depths = compute_lake_depth_grids(
                dem_array=dem_elev,
                transform=transform,
                seed_xy=seed_xy,
                wse_m=wse_m,
                barrier_mask=geometry_result["barrier_mask"],
                barrier_crest_m=barrier_crest_elev_m,
                levels_m=_lake_levels,
            )
            lake_meta["extent_provenance"] = "DEM_FILL_SOURCED_CREST"
            lake_meta["sill_merges"] = _lake_sills
            lake_meta["z_min_m"] = round(float(_lake_zmin), 2)
            lake_meta["crest_m"] = round(float(barrier_crest_elev_m), 2)

            # Timing. Constant inflow means volume accumulates linearly, so a
            # frame holding fraction f of the final pool sits at -(1-f) x the
            # formation time. The duration is SOURCED or it is declared as not
            # sourced -- the old [-120, -60, -30, -10] was neither, while
            # `formation_time_h` sat in the scenario config read by nothing.
            _lf_cfg = sc_cfg.get("lake_formation") or {}
            _ft_h = _lf_cfg.get("formation_time_h")
            _v_full = max((g["volume_m3"] for g in lake_depths), default=0.0)
            if _ft_h and _v_full > 0:
                _total_min = float(_ft_h) * 60.0
                lake_dt_mins = [-_total_min * (1.0 - g["volume_m3"] / _v_full)
                                for g in lake_depths]
                lake_meta["time_provenance"] = "SOURCED_DURATION_ASSUMED_CONSTANT_INFLOW"
                lake_meta["time_note"] = (
                    f"duration {_ft_h} h sourced from "
                    f"SCENARIOS['{scenario_key}']['lake_formation']['formation_time_h']; "
                    f"constant inflow assumed within it")
            else:
                # No sourced duration. Say so rather than inventing one; the
                # frames are still equal-volume and still worth showing.
                _total_min = 120.0
                _n = max(1, len(lake_depths))
                lake_dt_mins = [-_total_min * (1.0 - (i + 1) / _n)
                                for i in range(_n)]
                lake_meta["time_provenance"] = "NO_SOURCED_DURATION_DECLARED_WINDOW"
                lake_meta["time_note"] = (
                    f"no formation_time_h for '{scenario_key}'; frames are spaced "
                    f"over a DECLARED {_total_min:.0f} min window that is NOT sourced. "
                    f"Levels are equal-volume and are independent of this choice.")
                logger.warning(
                    "M2: '%s' has no sourced formation_time_h - lake-formation "
                    "frame TIMES are a declared %.0f min window, not sourced",
                    scenario_key, _total_min)
            logger.info(
                "M2: lake formation - %d equal-volume frames, %.2f -> %.2f MCM, "
                "levels %.1f -> %.1f m, %d sill merge(s), timing %s",
                len(lake_depths), lake_depths[0]["volume_m3"] / 1e6,
                _v_full / 1e6, lake_depths[0]["level_m"], lake_depths[-1]["level_m"],
                len(_lake_sills), lake_meta["time_provenance"])
        for k, lk in enumerate(lake_depths):
            t_min = lake_dt_mins[k] if k < len(lake_dt_mins) else -10.0
            t_s = t_min * 60.0
            fp = snapshots_dir / f"frame_pre_{k:02d}.geojson"
            preview_path = preview_dir / f"frame_pre_{k:02d}.png"
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(_depth_to_geojson(lk["depth_grid"], transform, t_s, to_wgs84), f)
            preview_bounds = _write_depth_preview(lk["depth_grid"], preview_path, transform, to_wgs84)
            # 1e: Tag cascade frames as "reservoir_rise" with contextual title.
            if cascade_cfg is not None:
                stage_label = "reservoir_rise"
                elev = lk["level_m"]
                z_crest_cfg = float(cascade_cfg["reservoir"]["z_crest_m"])
                if elev < z_crest_cfg - 2.0:
                    phase_title = f"Reservoir Rising — surge en route ({elev:.0f} m)"
                elif elev < z_crest_cfg:
                    phase_title = f"Approaching Crest ({elev:.1f} m / {z_crest_cfg:.0f} m)"
                else:
                    phase_title = f"Overtopping Threshold ({elev:.1f} m)"
            else:
                stage_label = "lake_formation"
                # `lk["fraction"]` is a STAGE fraction -- the share of the HEIGHT
                # range, not of the water. Labelling it "% Capacity" is how the
                # UI came to show "25% Capacity" on a frame holding 1.8 % of the
                # pool. Report the share of the impounded VOLUME instead, and
                # name it as such.
                _v_ref = max((g["volume_m3"] for g in lake_depths), default=0.0)
                _share = (100.0 * lk["volume_m3"] / _v_ref) if _v_ref > 0 else 0.0
                phase_title = f"Lake Formation ({_share:.0f}% of pool volume)"
            fr_entry = {
                "t_s": t_s,
                "t_min": t_min,
                "stage": stage_label,
                "phase_title": phase_title,
                "volume_mcm": round(lk["volume_m3"] / 1e6, 2),
                "area_km2": round(lk["area_m2"] / 1e6, 2),
                "level_m": round(lk["level_m"], 1),
                "level_source": level_source,
                "path": str(fp),
                "preview_path": str(preview_path),
                "preview_bounds": preview_bounds,
            }
            lake_frames.append(fr_entry)
            lake_meta["stages"].append(fr_entry)
        with open(out_dir / "lake_formation.json", "w", encoding="utf-8") as f:
            json.dump(lake_meta, f, indent=2)
        logger.info("M2: Generated %d pre-breach lake formation snapshots", len(lake_frames))
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

    # Stage G: mass_balance() is retired as the physics gate. It computed
    # unaccounted = injected - stored and called that "outflow through open
    # boundaries + numerical clipping" -- one number standing for two
    # physically opposite things, so it cannot detect the solver creating
    # water (stored > injected) or tell real outflow apart from a leak.
    # SimulationResult.mass_closure() already tracks outflow and clipping as
    # separate running sums during integration and forms the correct
    # residual: total_in - stored - outflow + clipped. This is the real
    # ledger the audit found sitting unused three files away.
    mb = sim_res.mass_closure()
    logger.info("M4: mass balance (central) — injected %.3e m^3, stored %.3e m^3, "
                "outflow %.3e m^3, clipped %.3e m^3, closure %.4f%%",
                mb["injected_m3"], mb["stored_m3"], mb["outflow_m3"],
                mb["clipped_m3"], mb["relative_error"] * 100.0)

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
        inundation_raster=flood_depth_tif,   # P4: the reservoir is not flood
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
        max_depth_raster=flood_depth_tif, threshold_m=THRESH_CAR,
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
            # P4: subtract the t = 0 pool before deciding a cell was reached.
            # Without this every reservoir cell is "reached" in the first frame
            # and the arrival raster paints the impoundment as an instant
            # inundation at t = 0.
            d = np.maximum(d - _init_depth_grid, 0.0)
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
        return str(worst(Provenance.COMPUTED_LIVE, terrain_prov, vol_prov,
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
    # prove the model reproduces a real flood.  Both Derna (Copernicus EMS
    # EMSR696) and Annamayya (Sentinel-1A SAR, EVD-27) have independently
    # observed outcomes to score against; other scenarios report absence.
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

            try:
                arrivals_res = m10.compare_arrivals(
                    scenario_key, max_depth_tif,
                    raster_stack=raster_stack,
                    out_dir=out_dir,
                )
                if arrivals_res.get("available"):
                    observed_block["arrivals"] = arrivals_res
            except Exception as exc:                      # noqa: BLE001
                logger.warning("M10: arrival validation unavailable (%s)", exc)
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

    # ── Validity gate for the manifest system (P0-1) ──────────────────────────
    # geometry: the resolved breach coordinate is a finite number that lands
    # inside the DEM extent. Anything beyond that (DEM-crest reconciliation,
    # IoU checks) is a later phase, not this one.
    from src.m2_geometry.dem_utils import escape_head_4connected

    validity_reasons: list[str] = []
    geometry_ok = bool(np.isfinite(bx) and np.isfinite(by))
    if geometry_ok:
        _g_row, _g_col = rtransform.rowcol(transform, bx, by)
        geometry_ok = bool(0 <= _g_row < ny and 0 <= _g_col < nx_grid)
        if not geometry_ok:
            validity_reasons.append(
                f"resolved breach coordinate ({bx:.1f}, {by:.1f}) falls outside the DEM extent"
            )
    else:
        validity_reasons.append(
            f"resolved breach coordinate ({bx}, {by}) is not finite"
        )

    # physics: mass closure within the tolerance this codebase already applies
    # to SimulationResult.mass_closure()'s relative_error elsewhere
    # (tests/test_swe_gpu.py asserts relative_error < 0.01; Stage G retired
    # the old mass_balance() gate, which could not distinguish real outflow
    # from manufactured mass).
    _MASS_BALANCE_TOLERANCE = 0.01
    mass_closure_ok = bool(mb["relative_error"] < _MASS_BALANCE_TOLERANCE)
    if not mass_closure_ok:
        validity_reasons.append(
            f"mass balance relative error {mb['relative_error']:.4f} exceeds "
            f"tolerance {_MASS_BALANCE_TOLERANCE}"
        )

    # P0-2 (FS-04): the initial water level must not already sit above the
    # DEM's own barrier crest — that would mean the flood is caused by the
    # initial condition, not the breach. Checked against the pre-carve
    # elevation captured above. `dem_crest_along_axis is None` (no DEM cells
    # sampled — should not happen once bx/by resolve, but fails closed rather
    # than silently passing if it does) counts as a failure, not a pass.
    _FREEBOARD_M = 0.0
    # Two crests, and the water level has to clear BOTH.
    #
    # `dem_crest_along_axis` is the highest DEM cell the dam axis crosses -- in a
    # gorge that is the valley wall, not the structure. Checking only against it
    # lets a water level sit far above the dam and still pass, as long as it is
    # below the surrounding mountains -- on phutkal the DEM ridge along the axis
    # is 3989.38 m, roughly 184 m above the structure.
    #
    # SUPERSEDED STATE, kept because the record still quotes it: this comment
    # used to cite a configured `wse_m` of 3878.0 m against a sourced crest of
    # 3794.11 m, and derived phutkal's G1/G4 failures from that 84 m gap. Both
    # numbers are gone. The config now holds `wse_m == thalweg_m + dam_height_m
    # == 3736.11 + 69.0 == 3805.11`, and the manifest's re-sourced crest is
    # 3805.11 m -- the same value, so the freeboard is 0.0 m by construction.
    #
    # Measured 2026-09-18 against the current tree: the seeded pool at 3805.11 m
    # holds 27.14 / 27.41 / 27.18 MCM at coarsen 1 / 2 / 4 against a configured
    # 30.0, and G1 PASSES. At the old 3878.0 m the same fill returns ~2886 MCM
    # and is correctly rejected as unconfined, which is what drove the old
    # fallback to the TYPED `volume_mcm = 30.0` labelled PROXY. The old "basin
    # holds 18.98 MCM at the crest" reproduces exactly (18.984 MCM at coarsen 2)
    # once the crest is set back to 3794.11 -- it was a measurement of the old
    # crest, not of this one. The old "DEM pool 2.97e6 m^3 at coarsen 4" does
    # NOT reproduce at any resolution, level or barrier state tried.
    #
    # A pool above the structure's own crest is not impounded -- it has already
    # overtopped, and whatever floods downstream was not caused by the breach.
    _crest_checks = [("DEM crest along the dam axis", dem_crest_along_axis)]
    if barrier_crest_elev_m is not None:
        _crest_checks.append(("sourced barrier crest", float(barrier_crest_elev_m)))
    crest_ok = True
    for _label, _c in _crest_checks:
        if _c is None:
            crest_ok = False
            validity_reasons.append(
                f"initial WSE {wse_m:.1f} m cannot be checked: the {_label} is "
                f"UNKNOWN")
        elif wse_m > _c + _FREEBOARD_M:
            crest_ok = False
            validity_reasons.append(
                f"initial WSE {wse_m:.1f} m exceeds the {_label} "
                f"{_c:.2f} m by {wse_m - _c:.1f} m — the pool is not impounded "
                f"by this structure, so the flood would not be caused by the "
                f"breach"
            )
    # ── P1 gates G1-G3: three checks that can actually FAIL ──────────────────
    # The closure test above is a tautology. `mass_closure()` forms
    # `total_in - stored - outflow + clipped` where `total_in` counts BOTH the
    # initial condition and the injected hydrograph as inputs, and `clipped` is
    # added back as accounted mass. So any run in which water merely stays put
    # scores ~0 regardless of whether the water had any right to be there:
    # measured, the production run (2x the impounded water), the Q=0 run and
    # the dry-bed run all report 0.00000 %. The three gates below are
    # independent of it and each one fails on a defect the closure cannot see.

    # G1/G2 and their tolerances live at module level (see `gate_g1`/`gate_g2`)
    # so the tests assert against these gates rather than against copies.
    _G1_TOLERANCE = G1_TOLERANCE
    _G2_TOLERANCE = G2_TOLERANCE

    # G1 -- volume provenance. Every cubic metre in the domain must come from
    # the impoundment we claim failed. `initial + injected` is the whole supply
    # side of the ledger; `impounded_vol_m3` is what the reservoir held. The
    # C1 double count (the pool placed as `initial_depth` AND re-supplied as
    # the injected hydrograph) makes this exactly 2.00 and nothing else in the
    # pipeline notices.
    g1_total_supplied_m3 = float(mb["initial_m3"] + mb["injected_m3"])
    g1_ok, g1_ratio = gate_g1(mb["initial_m3"], mb["injected_m3"], impounded_vol_m3)
    if impounded_vol_m3 is None:
        validity_reasons.append(
            "G1 volume provenance: the impoundment was never sized, so there is "
            "nothing to check the domain's water against")
    elif not g1_ok:
        validity_reasons.append(
            f"G1 volume provenance: the domain was supplied "
            f"{g1_total_supplied_m3:.4e} m^3 against an impounded volume of "
            f"{impounded_vol_m3:.4e} m^3 (ratio {g1_ratio:.3f}, tolerance "
            f"{1 - _G1_TOLERANCE:.2f}-{1 + _G1_TOLERANCE:.2f}) — water in the "
            f"flood did not come from the impoundment"
        )

    # G2 -- manufactured mass. Negative depths produced by the scheme are
    # clamped to zero and the clamped volume booked into `clipped`, where the
    # closure identity ADDS it back as if it were accounted for. It is not
    # accounted for: it is water invented by the discretisation. Here it is a
    # failure in its own right, measured against the input it is a fraction of.
    g2_total_in_m3 = float(mb["initial_m3"] + mb["injected_m3"])
    g2_ok, g2_ratio = gate_g2(mb["clipped_m3"], mb["initial_m3"], mb["injected_m3"])
    if not g2_ok:
        validity_reasons.append(
            f"G2 manufactured mass: clipping created {mb['clipped_m3']:.4e} m^3, "
            f"{g2_ratio * 100:.3f} % of the {g2_total_in_m3:.4e} m^3 supplied "
            f"(tolerance {_G2_TOLERANCE * 100:.1f} %)"
        )

    # G3 -- reachable outlet. Part II opened the domain; nothing asserted that
    # it stayed open at the resolution actually run. The threshold is the
    # impoundment's own head: if the water has to pond deeper than the entire
    # dam height before any of it can leave the grid, the domain is sealed for
    # this event no matter what the outlet report says. Measured before Part
    # II's outlet fix: 1101-1572 m of escape head against dam heights of
    # 58-70 m. After it: 6.6-36.5 m.
    g3_escape_head_m = None
    g3_ok = False
    if geometry_ok:
        try:
            _esc_level = escape_head_4connected(dem_elev, int(_g_row), int(_g_col))
            g3_escape_head_m = float(_esc_level - float(dem_elev[int(_g_row), int(_g_col)]))
            g3_ok = bool(dam_height is not None and np.isfinite(g3_escape_head_m)
                         and g3_escape_head_m < float(dam_height))
        except Exception as exc:                                  # noqa: BLE001
            validity_reasons.append(f"G3 reachable outlet: could not be evaluated ({exc})")
    if not g3_ok and g3_escape_head_m is not None:
        validity_reasons.append(
            f"G3 reachable outlet: water must pond {g3_escape_head_m:.1f} m above the "
            f"release cell before it can reach the domain edge across cell faces, "
            f"against a {dam_height if dam_height is None else round(dam_height, 1)} m "
            f"impoundment head — the domain is sealed "
            f"for this event"
        )
    elif not g3_ok and g3_escape_head_m is None and geometry_ok:
        validity_reasons.append(
            "G3 reachable outlet: the release cell does not resolve on the grid"
        )

    # G4 -- the impoundment must hold. `validate_geometry` measures the level at
    # which water first escapes the upstream basin, on the terrain WITH the
    # barrier emplaced and with the face-averaged bed `swe_2d._rhs` actually
    # integrates. Two distinct defects show up here and both are fatal to the
    # meaning of a run: a barrier under two cells thick (the face average erases
    # a one-cell wall entirely -- measured on a synthetic 28 m grid, a one-cell
    # wall passes 46.7 % of the pool in 300 s against 0.000 m^3 for two cells),
    # and a basin whose own rim sits below the level the scenario fills it to.
    # Measured: rishiganga escapes at 2162.89 m against a configured 2175.68 m
    # (12.79 m over) and phutkal at 3779.07 m against 3794.11 m (15.04 m over).
    # That is why the audit's E5 experiment saw the Q=0 pool spread from 35 to
    # 195 wet cells -- not a leaking dam, a pool filled above its own rim.
    g4_spill_level_m = geometry_result.get("spill_level_m")
    g4_shortfall_m = (float(wse_m) - float(g4_spill_level_m))         if g4_spill_level_m is not None else None
    g4_ok = bool(g4_shortfall_m is not None and np.isfinite(g4_shortfall_m)
                 and g4_shortfall_m <= 0.0)
    if not g4_ok:
        validity_reasons.append(
            f"G4 impoundment retention: water escapes the upstream basin at "
            f"{g4_spill_level_m if g4_spill_level_m is None else round(g4_spill_level_m, 2)} m "
            f"against a water level of {wse_m:.2f} m — the pool is "
            f"{g4_shortfall_m if g4_shortfall_m is None else round(g4_shortfall_m, 2)} m "
            f"above what this basin holds at {dx_m:.1f} m resolution, so it spreads "
            f"downhill whether or not the barrier ever fails"
        )

    # G5 -- the release must actually have happened. Q is an OUTPUT now (audit
    # §38.4), which means it is also evidence: if the pool sits above the breach
    # invert and the measured discharge is identically zero, nothing was
    # simulated, whatever the other gates say. Every gate above can pass on a
    # run in which no water ever moved -- G1 checks where the water CAME from,
    # G2 that none was invented, G3 that an exit exists, G4 that the pool is
    # held. None of them asks whether the dam broke.
    #
    # Measured on phutkal at coarsen 4 (2026-09-13): a live opening was
    # reported, G2 and G3 passed, and the measured peak was 0 m^3/s because the
    # 37.7 m breach was narrower than the 111.6 m cell and never cut. That run
    # is now refused at the opening; this gate is the backstop for every other
    # way a release can come out empty.
    g5_measured_peak_m3s = None
    g5_ok = True
    if opening_report.get("active"):
        _q = sim_res.breach_q_m3s or []
        g5_measured_peak_m3s = float(max(_q)) if _q else 0.0
        g5_ok = bool(g5_measured_peak_m3s > 0.0)
        if not g5_ok:
            validity_reasons.append(
                f"G5 release actually occurred: the breach opening is active "
                f"(crest {opening_report.get('crest_elev_m')} m, invert "
                f"{opening_report.get('invert_final_m')} m, B="
                f"{opening_report.get('final_width_m')} m) but the measured "
                f"discharge peaks at {g5_measured_peak_m3s:.4g} m^3/s — no water "
                f"left the impoundment, so this run did not simulate a dam break"
            )
    elif opening_report.get("reason"):
        g5_ok = False
        validity_reasons.append(
            f"G5 release actually occurred: no breach opening could be built "
            f"({opening_report['reason']}) — the run reports zero release"
        )

    physics_ok = bool(mass_closure_ok and crest_ok and g1_ok and g2_ok
                      and g3_ok and g4_ok and g5_ok)

    sources_ok = terrain_prov is not Provenance.SYNTHETIC_TERRAIN
    if not sources_ok:
        validity_reasons.append(f"terrain provenance is {terrain_prov}, not a surveyed DEM")

    # ── Flow-regime gate ──────────────────────────────────────────────────────
    # The solver integrates the clear-water shallow-water equations: constant
    # density, a single Manning n, a fixed bed, no solid phase. For an event
    # whose governing physics is a granular debris flow that is not a
    # calibration difference, it is a different system of equations — see
    # m3_breach.DEBRIS_FLOW_MISSING_PHYSICS for the state variables and
    # closures that are absent. Raising Q or n cannot substitute for them, so
    # the honest outcome is a run that refuses to be called valid rather than
    # one that presents a water-only analogue as the event.
    #
    # HYPERCONCENTRATED does not fail: solids at that concentration still
    # behave Newtonian enough for SWE, the bias (depth and momentum read low)
    # is stated, and the run stays usable.
    from src.m3_breach import parse_flow_regime, regime_status
    flow_regime = parse_flow_regime(sc_cfg.get("flow_regime"))
    regime_block = regime_status(flow_regime)
    regime_ok = bool(regime_block["applicable"] or regime_block["approximate"])
    if not regime_ok:
        validity_reasons.append(
            f"flow regime {flow_regime} is outside the clear-water SWE solver's "
            f"envelope — missing: {'; '.join(regime_block['missing_physics'][:3])}, "
            f"and {len(regime_block['missing_physics']) - 3} more"
        )
    elif regime_block["approximate"]:
        logger.warning("REGIME: %s — %s", flow_regime, regime_block["note"])

    artifact_manifest = []
    if Path(results_geojson_path).is_file():
        artifact_manifest.append({"name": "results", "path": str(results_geojson_path), "media_type": "application/geo+json", "required": True})
    if Path(max_depth_tif).is_file():
        artifact_manifest.append({"name": "max_depth", "path": str(max_depth_tif), "media_type": "image/tiff", "required": True})
    if Path(out_dir / "hydrograph.json").is_file():
        artifact_manifest.append({"name": "hydrograph", "path": str(out_dir / "hydrograph.json"), "media_type": "application/json", "required": True})
    if snapshots_index_path and Path(snapshots_index_path).is_file():
        artifact_manifest.append({"name": "snapshots_index", "path": str(snapshots_index_path), "media_type": "application/json", "required": True})
    if roads_timeline_path and Path(roads_timeline_path).is_file():
        artifact_manifest.append({"name": "roads_timeline", "path": str(roads_timeline_path), "media_type": "application/geo+json", "required": False})
    if 'envelope_tif' in locals() and envelope_tif and Path(envelope_tif).is_file():
        artifact_manifest.append({"name": "envelope", "path": str(envelope_tif), "media_type": "image/tiff", "required": False})
    if 'envelope_geojson_path' in locals() and envelope_geojson_path and Path(envelope_geojson_path).is_file():
        artifact_manifest.append({"name": "envelope_geojson", "path": str(envelope_geojson_path), "media_type": "application/geo+json", "required": False})
    if shp_path and Path(shp_path).is_file():
        artifact_manifest.append({"name": "shp", "path": str(shp_path), "media_type": "application/octet-stream", "required": False})
    if kml_path and Path(kml_path).is_file():
        artifact_manifest.append({"name": "kml", "path": str(kml_path), "media_type": "application/vnd.google-earth.kml+xml", "required": False})
    if cap_path and Path(cap_path).is_file():
        artifact_manifest.append({"name": "cap", "path": str(cap_path), "media_type": "application/json", "required": False})

    valid = bool(
        geometry_ok and physics_ok and sources_ok and regime_ok
        and len([a for a in artifact_manifest if a["required"]]) > 0
    )

    return {
        "status": "done",
        "scenario_name": dam_name,
        "results_geojson": str(results_geojson_path),
        "hydrograph_json": str(out_dir / "hydrograph.json"),
        "max_depth_tif": str(max_depth_tif),
        "envelope_tif": str(envelope_tif) if 'envelope_tif' in locals() else None,
        "envelope_geojson": str(envelope_geojson_path) if 'envelope_geojson_path' in locals() else None,
        "arms_contributing": arms_contributing if 'arms_contributing' in locals() else None,
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
        "volume_provenance": str(vol_prov),
        "geometry_diagnostic": geometry_diagnostic,
        "flood_depth_tif": str(flood_depth_tif),
        "reservoir_exclusion": reservoir_exclusion,
        "barrier_emplacement": barrier_emplacement,
        "breach_opening": opening_report,
        "river_outlet": outlet_report,
        "flowline_conditioning": flowline_report,
        "channel_roughness": channel_roughness_report,
        "flow_regime": regime_block,
        "lake_formation": str(out_dir / "lake_formation.json") if (out_dir / "lake_formation.json").exists() else None,
        "validation": validation,
        "validity": {
            "valid": bool(valid),
            "geometry": bool(geometry_ok),
            "physics": bool(physics_ok),
            "sources": bool(sources_ok),
            "regime": bool(regime_ok),
            "flow_regime": str(flow_regime),
            "required_artifacts": [a["name"] for a in artifact_manifest if a["required"]],
            "reasons": validity_reasons,
            "initial_wse_m": round(wse_m, 2),
            "initial_condition_source": initial_condition_source,
            "dem_crest_along_axis_m": round(dem_crest_along_axis, 2) if dem_crest_along_axis is not None else None,
            "mass_balance_relative_error": mb["relative_error"],
            "gate_g1_volume_provenance": {
                "ok": bool(g1_ok), "supplied_m3": g1_total_supplied_m3,
                "impounded_m3": None if impounded_vol_m3 is None else float(impounded_vol_m3),
                "ratio": None if not np.isfinite(g1_ratio) else float(g1_ratio),
                "tolerance": _G1_TOLERANCE,
            },
            "gate_g2_manufactured_mass": {
                "ok": bool(g2_ok), "clipped_m3": float(mb["clipped_m3"]),
                "fraction_of_input": float(g2_ratio),
                "tolerance": _G2_TOLERANCE,
            },
            "gate_g3_reachable_outlet": {
                "ok": bool(g3_ok), "escape_head_m": g3_escape_head_m,
                "threshold_m": None if dam_height is None else float(dam_height),
            },
            "gate_g4_impoundment_retention": {
                "ok": bool(g4_ok),
                "spill_level_m": g4_spill_level_m,
                "water_level_m": float(wse_m),
                "shortfall_m": g4_shortfall_m,
            },
            "gate_g5_release_occurred": {
                "ok": bool(g5_ok),
                "opening_active": bool(opening_report.get("active")),
                "measured_peak_q_m3s": g5_measured_peak_m3s,
                "cells_opening_at_full_width":
                    opening_report.get("cells_opening_at_full_width"),
            },
            "outflow_gross_m3": mb.get("outflow_gross_m3"),
            "river_outlets_opened": outlet_report.get("outlets"),
            "flowline_cells_lowered": flowline_report.get("cells_lowered"),
            "flowline_max_drop_m": flowline_report.get("max_drop_m"),
            "channel_cells": channel_roughness_report.get("channel_cells"),
        },
        "artifact_manifest": artifact_manifest,
    }


if __name__ == "__main__":
    from src.data_fetcher import SCENARIOS
    parser = argparse.ArgumentParser(description="FloodSight simulation pipeline")
    parser.add_argument("--dam-name", default=None)
    parser.add_argument("--scenario", default="phutkal",
                        choices=list(SCENARIOS.keys()))
    parser.add_argument("--wse", type=float, default=None,
                        help="REFUSED. The water level is derived from the "
                             "scenario's sourced thalweg + dam height and "
                             "clamped to the sourced crest; supplying one "
                             "raises rather than being silently ignored.")
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
    parser.add_argument("--lulc-path", default=None, help="Path to ESA WorldCover / LULC land cover GeoTIFF")
    parser.add_argument("--population-csv", default=None, help="Path to local surveyed population CSV")
    args = parser.parse_args()

    sc_info = SCENARIOS.get(args.scenario, {})
    dam_name = args.dam_name or sc_info.get("name", f"{args.scenario.capitalize()} Scenario")
    # Do NOT fall back to the scenario's own `wse_m` here. That fallback meant
    # the CLI always handed `execute_full_simulation` a concrete level, so the
    # parameter looked live from the outside while being overwritten inside.
    # None is the only value the pipeline accepts; `--wse` now raises.
    wse_m = args.wse
    out_dir = args.out_dir or f"data/scenarios/{args.scenario}_real"
    duration_s = args.duration or (20000.0 if args.scenario == "annamayya" else 7200.0)

    result = execute_full_simulation(
        dam_name=dam_name, scenario_key=args.scenario, wse_m=wse_m,
        failure_mode=args.failure_mode, reservoir_fill=args.reservoir_fill,
        out_dir=out_dir, total_duration_s=duration_s,
        allow_synthetic=args.offline_demo, coarsen=args.coarsen,
        custom_dem_path=args.custom_dem, crest_length_m=args.crest_length,
        dam_type=args.dam_type,
        lulc_raster_path=args.lulc_path, population_csv=args.population_csv,
    )

    # Persist the run's own verdict. The CLI used to DISCARD this return value
    # entirely, so gates G1-G5 -- the whole point of the validity machinery --
    # were computed and thrown away on every command-line run, and only the API
    # worker path ever recorded them. A gate whose result nobody can read is
    # indistinguishable from no gate.
    prov_path = Path(out_dir) / "run_provenance.json"
    prov_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    v = (result or {}).get("validity", {})
    logger.info("=" * 60)
    logger.info("VALIDITY: %s", "VALID" if v.get("valid") else "NOT VALID")
    for _g in ("gate_g1_volume_provenance", "gate_g2_manufactured_mass",
               "gate_g3_reachable_outlet", "gate_g4_impoundment_retention",
               "gate_g5_release_occurred"):
        _b = v.get(_g)
        if isinstance(_b, dict):
            logger.info("  %-34s %s", _g.split("_", 2)[1].upper() + ":",
                        "pass" if _b.get("ok") else "FAIL")
    for _r in v.get("reasons", []):
        logger.info("  reason: %s", _r)
    logger.info("  written to %s", prov_path)
    logger.info("=" * 60)
