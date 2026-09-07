"""
FloodSight — FastAPI Application with Real Hydrodynamic Execution
=================================================================
Backend API providing real-time 2D shallow water hydrodynamic dam-break modeling.

Endpoints:
- POST /api/run                    → trigger live simulation
- GET  /api/status/{job_id}        → status of live job (includes cell_size_m)
- GET  /api/results/{job_id}       → GeoJSON results of village inundation
- GET  /api/hydrograph/{job_id}    → Q(t) outflow hydrograph with confidence bands
- GET  /api/export/{job_id}/{fmt}  → download .shp, .kml, CAP alert JSON, or GeoTIFF
- GET  /api/roads/{job_id}         → roads_timeline.geojson with per-link cut_time_min
- GET  /api/arrival/{job_id}       → arrival_time.tif (values in minutes, WGS84)
"""

from __future__ import annotations

import asyncio
import json
import logging
import locale
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import sys

# Ensure run_pipeline can be imported from project root
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from run_pipeline import execute_full_simulation

logger = logging.getLogger("floodsight.api")
logging.basicConfig(level=logging.INFO)


def _read_json_file(path: str | Path):
    """Load JSON from disk regardless of whether the file was written in UTF-8 or cp1252."""
    p = Path(path)
    raw = p.read_bytes()

    for enc in ("utf-8", "utf-8-sig", "cp1252", locale.getpreferredencoding(False), "latin-1"):
        if not enc:
            continue
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue

    # Final fallback: try the platform default and then strict UTF-8 only.
    text = raw.decode("utf-8", errors="replace")
    return json.loads(text)

app = FastAPI(
    title="FloodSight API",
    description="Rapid consequence-assessment engine for unmapped impoundments (SIH26161)",
    version="1.0.0",
)

_JOBS: dict[str, dict] = {}

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
FRONT_DIR = BASE_DIR / "frontend"


def _detect_scenario_key_and_name(p: Path):
    name = p.name.lower()
    sn_file = p / "snapshots_index.json"
    res_file = p / "results.geojson"
    
    # 1. Direct name match
    if "south_lhonak" in name:
        return "south_lhonak", "South Lhonak GLOF & Chungthang Dam"
    if "derna" in name:
        return "derna", "Derna Dams (Abu Mansour + Al-Bilad)"
    if "rishi" in name:
        return "rishiganga", "Rishi Ganga Natural Lake"
    if "annamayya" in name:
        return "annamayya", "Annamayya Dam (Cheyyeru River)"
    if "phutkal" in name:
        return "phutkal", "Phutkal River Landslide Dam"
    if "ivanovo" in name:
        return "ivanovo", "Ivanovo Dam (Bulgaria)"
    if "malpasset" in name:
        return "malpasset", "Malpasset Arch Dam (France)"
    
    # 2. Check preview_bounds in snapshots_index.json
    if sn_file.exists():
        try:
            sn_data = _read_json_file(sn_file)
            if sn_data and len(sn_data) > 0:
                bounds = sn_data[0].get("preview_bounds", [])
                if bounds and len(bounds) > 0:
                    lon, lat = bounds[0][0], bounds[0][1]
                    if 78.5 <= lon <= 79.4 and 14.0 <= lat <= 14.6:
                        return "annamayya", "Annamayya Dam (Cheyyeru River)"
                    if 79.4 <= lon <= 80.2 and 30.2 <= lat <= 30.8:
                        return "rishiganga", "Rishi Ganga Natural Lake"
                    if 76.5 <= lon <= 77.4 and 33.1 <= lat <= 33.7:
                        return "phutkal", "Phutkal River Landslide Dam"
                    if 22.0 <= lon <= 23.0 and 32.4 <= lat <= 33.0:
                        return "derna", "Derna Dams (Abu Mansour + Al-Bilad)"
                    if 25.5 <= lon <= 26.1 and 41.6 <= lat <= 42.2:
                        return "ivanovo", "Ivanovo Dam (Bulgaria)"
                    if 6.4 <= lon <= 7.0 and 43.3 <= lat <= 43.8:
                        return "malpasset", "Malpasset Arch Dam (France)"
                    if 88.0 <= lon <= 88.8 and 27.4 <= lat <= 28.2:
                        return "south_lhonak", "South Lhonak GLOF & Chungthang Dam"
        except Exception:
            pass

    # 3. Check results.geojson
    if res_file.exists():
        try:
            res_data = _read_json_file(res_file)
            feats = res_data.get("features", [])
            if feats:
                geom = feats[0].get("geometry", {})
                coords = geom.get("coordinates", [])
                pt = None
                if geom.get("type") == "Point": pt = coords
                elif geom.get("type") == "Polygon" and coords: pt = coords[0][0]
                elif geom.get("type") == "MultiPolygon" and coords: pt = coords[0][0][0]
                if pt:
                    lon, lat = pt[0], pt[1]
                    if 78.5 <= lon <= 79.4 and 14.0 <= lat <= 14.6:
                        return "annamayya", "Annamayya Dam (Cheyyeru River)"
                    if 79.4 <= lon <= 80.2 and 30.2 <= lat <= 30.8:
                        return "rishiganga", "Rishi Ganga Natural Lake"
                    if 76.5 <= lon <= 77.4 and 33.1 <= lat <= 33.7:
                        return "phutkal", "Phutkal River Landslide Dam"
                    if 22.0 <= lon <= 23.0 and 32.4 <= lat <= 33.0:
                        return "derna", "Derna Dams (Abu Mansour + Al-Bilad)"
                    if 25.5 <= lon <= 26.1 and 41.6 <= lat <= 42.2:
                        return "ivanovo", "Ivanovo Dam (Bulgaria)"
                    if 6.4 <= lon <= 7.0 and 43.3 <= lat <= 43.8:
                        return "malpasset", "Malpasset Arch Dam (France)"
                    if 88.0 <= lon <= 88.8 and 27.4 <= lat <= 28.2:
                        return "south_lhonak", "South Lhonak GLOF & Chungthang Dam"
        except Exception:
            pass
    return "phutkal", f"Scenario {p.name}"


def _rehydrate_saved_scenarios():
    """Scan data/scenarios on disk and populate _JOBS with completed runs so past simulations survive restarts."""
    scenarios_dir = DATA_DIR / "scenarios"
    if not scenarios_dir.exists():
        return

    count = 0
    for p in scenarios_dir.iterdir():
        if not p.is_dir():
            continue
        job_id = p.name
        if job_id in _JOBS:
            continue
        res_file = p / "results.geojson"
        if not res_file.exists():
            continue

        try:
            hyd_file = p / "hydrograph.json"
            hyd = _read_json_file(hyd_file) if hyd_file.exists() else {}

            exports_dir = p / "exports"
            shp_path = None
            kml_path = None
            cap_path = None
            if exports_dir.exists():
                for f in exports_dir.iterdir():
                    if f.suffix == ".shp" and not shp_path:
                        shp_path = str(f)
                    elif f.suffix == ".kml" and not kml_path:
                        kml_path = str(f)
                    elif f.suffix == ".xml" and not cap_path:
                        cap_path = str(f)

            max_depth_tif = p / "max_depth.tif"
            snapshots_idx = p / "snapshots_index.json"
            roads_timeline = p / "roads_timeline.geojson"
            arrival_tif = p / "arrival_time.tif"
            lake_form = p / "lake_formation.json"

            tot_par = 0
            tot_bld = 0
            tot_loss = 0.0
            try:
                res_data = _read_json_file(res_file)
                for feat in res_data.get("features", []):
                    props = feat.get("properties", {})
                    tot_par += int(props.get("pop_at_risk") or 0)
                    tot_bld += int(props.get("buildings_flooded") or 0)
                    tot_loss += float(props.get("loss_inr") or 0.0)
            except Exception:
                pass

            s_key, s_name = _detect_scenario_key_and_name(p)

            _JOBS[job_id] = {
                "status": "done",
                "job_id": job_id,
                "dam_name": s_name,
                "scenario_key": s_key,
                "result_path": str(res_file),
                "hydrograph": hyd,
                "exports": {
                    "shp": shp_path,
                    "kml": kml_path,
                    "cap": cap_path,
                    "tif": str(max_depth_tif) if max_depth_tif.exists() else None,
                },
                "metrics": {
                    "total_par": tot_par,
                    "total_buildings": tot_bld,
                    "total_loss_inr": tot_loss,
                },
                "snapshots_index": str(snapshots_idx) if snapshots_idx.exists() else None,
                "lake_formation": str(lake_form) if lake_form.exists() else None,
                "roads_timeline": str(roads_timeline) if roads_timeline.exists() else None,
                "arrival_time_tif": str(arrival_tif) if arrival_tif.exists() else None,
                "mtime": p.stat().st_mtime,
                "progress": {
                    "stage": "done",
                    "label": "Complete",
                    "frac": 1.0,
                    "detail": "Loaded from archive",
                    "eta_s": 0,
                },
            }
            count += 1
        except Exception as exc:
            logger.debug("Could not rehydrate scenario %s: %s", job_id, exc)

    logger.info("Rehydrated %d saved simulation scenarios into memory", count)


@app.on_event("startup")
async def startup_event():
    _rehydrate_saved_scenarios()


# Also rehydrate at import time so routes are immediately populated
_rehydrate_saved_scenarios()


class RunRequest(BaseModel):
    dam_name: str = "Phutkal River Landslide Dam 2015"
    scenario_key: str = "phutkal"
    wse_m: float = 3850.0
    failure_mode: str = "overtopping"
    reservoir_level_fraction: float = 0.9
    duration_s: float = 7200.0
    coarsen: int = 4
    custom_dem_path: Optional[str] = None
    crest_length_m: Optional[float] = None
    dam_type: Optional[str] = None
    spillway_capacity_m3s: Optional[float] = None
    lulc_raster_path: Optional[str] = None
    population_csv: Optional[str] = None


def _async_job_runner(job_id: str, req: RunRequest):
    try:
        _JOBS[job_id]["status"] = "computing_hydrodynamics"
        out_dir = DATA_DIR / "scenarios" / job_id

        def _on_progress(p: dict) -> None:
            frac = p.get("frac") or 0.0
            elapsed = p.get("elapsed_s") or 0.0
            eta = round(elapsed * (1.0 - frac) / frac, 1) if frac > 0.02 else None
            _JOBS[job_id]["progress"] = {**p, "eta_s": eta}

        result = execute_full_simulation(
            dam_name=req.dam_name,
            scenario_key=req.scenario_key,
            wse_m=req.wse_m,
            failure_mode=req.failure_mode,
            reservoir_fill=req.reservoir_level_fraction,
            out_dir=out_dir,
            total_duration_s=req.duration_s,
            coarsen=req.coarsen,
            progress_cb=_on_progress,
            custom_dem_path=req.custom_dem_path,
            crest_length_m=req.crest_length_m,
            dam_type=req.dam_type,
            spillway_capacity_m3s=req.spillway_capacity_m3s,
            lulc_raster_path=req.lulc_raster_path,
            population_csv=req.population_csv,
        )
        _JOBS[job_id]["status"] = "computing_exposure"

        hyd = _read_json_file(result["hydrograph_json"])

        _JOBS[job_id]["progress"] = {
            "stage": "done", "label": "Complete", "frac": 1.0,
            "detail": "", "eta_s": 0,
        }
        _JOBS[job_id].update({
            "status": "done",
            "result_path": result["results_geojson"],
            "hydrograph": hyd,
            "exports": {
                "shp": result["shp"],
                "kml": result["kml"],
                "cap": result["cap"],
                "tif": result["max_depth_tif"],
            },
            "metrics": {
                "total_par": result["total_par"],
                "total_buildings": result["total_buildings"],
                "total_loss_inr": result["total_loss_inr"],
            },
            "snapshots_index": result.get("snapshots_index"),
            "lake_formation": result.get("lake_formation"),
            "validation_agreement": result.get("validation_agreement"),
            "observed_extent": result.get("observed_extent"),
            "validation_roads": result.get("validation_roads"),
            "observed": result.get("observed"),
            "roads_timeline": result.get("roads_timeline"),
            "arrival_time_tif": result.get("arrival_time_tif"),
            "cell_size_m": result.get("cell_size_m"),
            "coarsen": result.get("coarsen"),
        })
        logger.info("Job %s completed successfully.", job_id)
    except Exception as exc:
        logger.exception("Job %s failed with error: %s", job_id, exc)
        _JOBS[job_id]["status"] = "error"
        _JOBS[job_id]["error"] = str(exc)


@app.post("/api/run")
async def run_simulation_endpoint(req: RunRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())[:8]
    _JOBS[job_id] = {
        "status": "queued",
        "dam_name": req.dam_name,
        "scenario_key": req.scenario_key,
    }
    background_tasks.add_task(_async_job_runner, job_id, req)
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    # Expose selected fields; omit large internal paths but include cell_size_m
    # so the frontend can display it in the header badge (D7 fix).
    expose_keys = {"status", "dam_name", "scenario_key", "error",
                   "cell_size_m", "coarsen", "metrics", "progress"}
    return {k: v for k, v in job.items() if k in expose_keys}


@app.get("/api/results/{job_id}")
async def get_results(job_id: str):
    job = _JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="Results not ready")
    path = job.get("result_path")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="Result file missing")
    return _read_json_file(path)


@app.get("/api/hydrograph/{job_id}")
async def get_hydrograph(job_id: str):
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.get("hydrograph", {})


@app.get("/api/export/{job_id}/{fmt}")
async def download_export(job_id: str, fmt: str):
    job = _JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="Exports not ready")
    exports = job.get("exports", {})
    path = exports.get(fmt)
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail=f"Export format '{fmt}' not found")
    return FileResponse(path, filename=Path(path).name)


@app.get("/api/snapshots/{job_id}")
async def get_snapshots(job_id: str):
    """Return the depth-snapshot frame index for the time-scrubber."""
    job = _JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="Snapshots not ready")
    idx_path = job.get("snapshots_index")
    if not idx_path or not Path(idx_path).exists():
        raise HTTPException(status_code=404, detail="Snapshot index missing")
    frames = _read_json_file(idx_path)
    return {"job_id": job_id, "frame_count": len(frames), "frames": frames}


@app.get("/api/snapshots/{job_id}/frame/{frame_idx}")
async def get_snapshot_frame(job_id: str, frame_idx: int):
    """Return one GeoJSON frame by index."""
    job = _JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="Snapshots not ready")
    idx_path = job.get("snapshots_index")
    if not idx_path or not Path(idx_path).exists():
        raise HTTPException(status_code=404, detail="Snapshot index missing")
    frames = _read_json_file(idx_path)
    if frame_idx < 0 or frame_idx >= len(frames):
        raise HTTPException(status_code=404, detail=f"Frame {frame_idx} out of range")
    frame_path = frames[frame_idx]["path"]
    if not Path(frame_path).exists():
        raise HTTPException(status_code=404, detail="Frame file missing")
    return _read_json_file(frame_path)


@app.get("/api/snapshots/{job_id}/frame/{frame_idx}/raster")
async def get_snapshot_raster(job_id: str, frame_idx: int):
    """Return the smooth RGBA preview texture for one real depth frame."""
    job = _JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="Snapshots not ready")
    idx_path = job.get("snapshots_index")
    if not idx_path or not Path(idx_path).exists():
        raise HTTPException(status_code=404, detail="Snapshot index missing")
    frames = _read_json_file(idx_path)
    if frame_idx < 0 or frame_idx >= len(frames):
        raise HTTPException(status_code=404, detail=f"Frame {frame_idx} out of range")
    path = frames[frame_idx].get("preview_path")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="Snapshot raster missing")
    return FileResponse(path, media_type="image/png")


@app.get("/api/ritter/{job_id}")
async def get_ritter(job_id: str):
    """Return Ritter analytical validation data."""
    job = _JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="Job not ready")
    idx_path = job.get("snapshots_index")
    if not idx_path:
        raise HTTPException(status_code=404, detail="Result path unknown")
    ritter_path = Path(idx_path).parent / "ritter_validation.json"
    if not ritter_path.exists():
        raise HTTPException(status_code=404, detail="Ritter validation file not found")
    return _read_json_file(ritter_path)


@app.get("/api/lake_formation/{job_id}")
async def get_lake_formation(job_id: str):
    """Return pre-breach lake formation metadata for this job."""
    job = _job_or_404(job_id)
    lake_path = job.get("lake_formation")
    if not lake_path or not Path(lake_path).exists():
        idx = job.get("snapshots_index")
        if idx:
            alt = Path(idx).parent / "lake_formation.json"
            if alt.exists():
                return _read_json_file(alt)
        return {"available": False, "stages": []}
    return _read_json_file(lake_path)


@app.get("/api/scenarios/metadata")
async def get_scenarios_metadata():
    """Return all scenario definitions with cascading dams and triggers."""
    from src.data_fetcher import SCENARIOS
    return SCENARIOS


@app.get("/api/scenarios/{scenario_key}/latest_job")
async def get_latest_scenario_job(scenario_key: str):
    """Return the best pre-computed or latest simulation run for a given scenario key."""
    matching = [j for j in _JOBS.values() if j.get("scenario_key") == scenario_key and j.get("status") == "done"]
    if not matching:
        _rehydrate_saved_scenarios()
        matching = [j for j in _JOBS.values() if j.get("scenario_key") == scenario_key and j.get("status") == "done"]

    if not matching:
        raise HTTPException(status_code=404, detail=f"No completed simulation found for scenario {scenario_key}")

    # Honest selection: pick the most recent run based on modification timestamp
    matching.sort(key=lambda j: j.get("mtime", 0.0), reverse=True)
    latest_job = matching[0]

    jid = latest_job["job_id"]
    sn_path = latest_job.get("snapshots_index")
    frame_count = 0
    if sn_path and Path(sn_path).exists():
        try:
            sn_data = _read_json_file(Path(sn_path))
            frame_count = len(sn_data) if isinstance(sn_data, list) else 0
        except Exception:
            pass

    has_lake = bool(latest_job.get("lake_formation") and Path(latest_job["lake_formation"]).exists())
    has_roads = bool(latest_job.get("roads_timeline") and Path(latest_job["roads_timeline"]).exists())

    return {
        "status": "ok",
        "job_id": jid,
        "scenario_key": scenario_key,
        "dam_name": latest_job.get("dam_name"),
        "frame_count": frame_count,
        "has_lake_formation": has_lake,
        "has_roads_timeline": has_roads,
        "metrics": latest_job.get("metrics", {}),
    }


@app.get("/api/roads/{job_id}")
async def get_roads_timeline(job_id: str):
    """Return roads_timeline.geojson — per-link cut_time_min for the road status layer."""
    job = _JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="Roads timeline not ready")
    path = job.get("roads_timeline")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="roads_timeline.geojson not found")
    return _read_json_file(path)


@app.get("/api/arrival/{job_id}")
async def get_arrival_time_raster(job_id: str):
    """Return the arrival-time GeoTIFF (values in minutes, nodata where never wet)."""
    job = _JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="Arrival time raster not ready")
    path = job.get("arrival_time_tif")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="arrival_time.tif not found")
    return FileResponse(path, media_type="image/tiff",
                        filename=f"arrival_time_{job_id}.tif")

@app.get("/api/envelope/{job_id}")
async def get_envelope_raster(job_id: str):
    """Return the ensemble extent envelope GeoTIFF."""
    job = _JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="Envelope raster not ready")
    path = job.get("envelope_tif")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="envelope.tif not found")
    return FileResponse(path, media_type="image/tiff",
                        filename=f"envelope_{job_id}.tif")

@app.get("/api/envelope_geojson/{job_id}")
async def get_envelope_geojson(job_id: str):
    """Return the ensemble extent envelope GeoJSON."""
    job = _JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="Envelope GeoJSON not ready")
    path = job.get("envelope_geojson")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="envelope.geojson not found")
    return _read_json_file(path)


# ── M10: validation against observed outcomes ─────────────────────────────────
# A run with no observed counterpart returns `available: false`, never a score.
# "We have nothing to compare against" and "we scored zero" are different states
# and the API keeps them different.

def _job_or_404(job_id: str) -> dict:
    job = _JOBS.get(job_id)
    if not job:
        scenario_dir = DATA_DIR / "scenarios" / job_id
        if scenario_dir.exists() and (scenario_dir / "results.geojson").exists():
            from src import m10_validation as m10
            scen_key = "ivanovo"
            val_agree = scenario_dir / "validation_agreement.geojson"
            obs_ext = scenario_dir / "observed_extent.geojson"
            val_roads = scenario_dir / "validation_roads.geojson"
            max_depth = scenario_dir / "max_depth.tif"
            obs_data = None
            if max_depth.exists():
                for k in ["ivanovo", "derna", "malpasset"]:
                    if m10.available(k):
                        try:
                            cmp = m10.compare_extent(max_depth, k)
                            if cmp.overlapped:
                                scen_key = k
                                obs_data = {
                                    "available": True,
                                    "scenario": k,
                                    "overlapped": True,
                                    "threshold_m": 0.3,
                                    "extent": cmp.skill.as_dict(),
                                    "areas": cmp.areas_km2(),
                                    **(m10.describe(k) or {}),
                                }
                                break
                        except Exception:
                            pass
            job = {
                "status": "done",
                "scenario_key": scen_key,
                "result_path": str(scenario_dir / "results.geojson"),
                "snapshots_index": str(scenario_dir / "snapshots_index.json") if (scenario_dir / "snapshots_index.json").exists() else None,
                "lake_formation": str(scenario_dir / "lake_formation.json") if (scenario_dir / "lake_formation.json").exists() else None,
                "validation_agreement": str(val_agree) if val_agree.exists() else None,
                "observed_extent": str(obs_ext) if obs_ext.exists() else None,
                "validation_roads": str(val_roads) if val_roads.exists() else None,
                "roads_timeline": str(scenario_dir / "roads_timeline.geojson") if (scenario_dir / "roads_timeline.geojson").exists() else None,
                "observed": obs_data,
            }
            _JOBS[job_id] = job
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="Job not ready")
    return job


def _geojson_or_404(path: Optional[str], what: str) -> dict:
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail=f"{what} not available for this run")
    return _read_json_file(path)


@app.get("/api/validation/{job_id}")
async def get_validation(job_id: str):
    """
    Skill scores against the observed flood, when one exists for this scenario.

    Shape: ``{available, scenario, event, source, extent: {csi, pod, far, ...},
    areas: {...}, roads: {...}}``. Any score may be ``null``: an undefined ratio
    stays undefined rather than being rendered as zero.
    """
    job = _job_or_404(job_id)
    observed = job.get("observed")
    if not observed:
        return {"available": False, "scenario": job.get("scenario_key"),
                "reason": "no observed dataset is wired for this scenario"}
    return observed


@app.get("/api/validation/{job_id}/agreement")
async def get_validation_agreement(job_id: str):
    """Hit / miss / false-alarm polygons for the simulated-vs-observed overlay."""
    job = _job_or_404(job_id)
    return _geojson_or_404(job.get("validation_agreement"), "Agreement overlay")


@app.get("/api/validation/{job_id}/observed")
async def get_observed_extent(job_id: str):
    """The observed flood extent itself, as delineated by the source agency."""
    job = _job_or_404(job_id)
    return _geojson_or_404(job.get("observed_extent"), "Observed extent")


@app.get("/api/validation/{job_id}/roads")
async def get_validation_roads(job_id: str):
    """Observed road links tagged with the model's verdict for each."""
    job = _job_or_404(job_id)
    return _geojson_or_404(job.get("validation_roads"), "Road validation")


@app.get("/api/observed/scenarios")
async def list_observed_scenarios():
    """Which scenarios have an observed outcome on disk, for the UI to advertise."""
    from src import m10_validation as m10
    return {k: m10.describe(k) for k in m10.SOURCES if m10.available(k)}


# ── Context layers (rivers, buildings, villages, facilities) ──────────────────
# The frontend previously hard-coded "assets/phutkal_rivers.geojson", which 404'd
# (the files live under data/, not frontend/assets/) and pinned every scenario to
# Phutkal's geometry. Served per scenario instead.

_LAYER_FILES = {
    "rivers":     ("admin", "{k}_rivers.geojson"),
    "villages":   ("admin", "{k}_villages.geojson"),
    "facilities": ("admin", "{k}_facilities.geojson"),
    "buildings":  ("buildings", "{k}_buildings.geojson"),
}


@app.get("/api/layers/{scenario_key}/{kind}")
async def get_context_layer(scenario_key: str, kind: str):
    """
    Serve a cached context layer for a scenario.

    Both path segments are user-controlled and are used to build a filesystem
    path, so both are checked against a fixed allow-list rather than sanitised.
    An unknown scenario or kind is a 404, never a lookup.
    """
    from src.data_fetcher import SCENARIOS

    if scenario_key not in SCENARIOS:
        raise HTTPException(status_code=404, detail=f"Unknown scenario '{scenario_key}'")
    if kind == "observed":
        from src import m10_validation as m10
        if not m10.available(scenario_key):
            raise HTTPException(status_code=404, detail=f"No observed data for '{scenario_key}'")
        try:
            return m10.observed_extent_geojson(scenario_key)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    if kind == "sar":
        from src.gee_satellite import GEESatelliteAnalyzer
        sc = SCENARIOS.get(scenario_key)
        if not sc:
            raise HTTPException(status_code=404, detail="Scenario not found")
        sar_path = DATA_DIR / "satellite" / f"{scenario_key}_sentinel1_sar.geojson"
        if sar_path.exists():
            return _read_json_file(sar_path)
        analyzer = GEESatelliteAnalyzer()
        data = analyzer.fetch_sentinel1_flood_extent(
            bbox=sc["bbox"],
            start_date="2021-11-18",
            end_date="2021-11-20",
        )
        sar_path.parent.mkdir(parents=True, exist_ok=True)
        with open(sar_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return data

    if kind not in _LAYER_FILES:
        raise HTTPException(status_code=404, detail=f"Unknown layer '{kind}'")

    subdir, pattern = _LAYER_FILES[kind]
    path = DATA_DIR / subdir / pattern.format(k=scenario_key)
    if not path.exists():
        # Genuinely absent (not every AOI has buildings or facilities). An empty
        # collection is the honest answer: the layer exists and holds nothing.
        return {"type": "FeatureCollection", "features": []}
    return _read_json_file(path)


@app.get("/api/benchmarks/malpasset")
async def get_malpasset_benchmark():
    """Canonical dry-bed dam break benchmark data for Malpasset (1959)."""
    p = DATA_DIR / "validation" / "malpasset" / "malpasset_benchmark.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="Malpasset benchmark data missing")
    return _read_json_file(p)


# ── No stale frontend, ever ───────────────────────────────────────────────────
# The browser was caching map.js and re-running an old build after every fix,
# which produced console stack traces pointing at line numbers that no longer
# existed in the source. Debugging against a cached bundle wastes more time than
# the bytes are worth, so the frontend is served no-store.
@app.middleware("http")
async def _no_cache_frontend(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if not path.startswith("/api/") and (
        path == "/" or path.endswith((".js", ".css", ".html", ".geojson"))
    ):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


if FRONT_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONT_DIR), html=True), name="frontend")
