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

import json
import logging
import locale
import os
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import sys

# Ensure run_pipeline can be imported from project root
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from run_pipeline import execute_full_simulation
from src.run_manifest import (ManifestError, is_valid, latest_valid, load_manifest,
                              new_manifest, transition, write_manifest, resolve_artifact)

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
    title="FloodSense API",
    description="Rapid consequence-assessment engine for unmapped impoundments (SIH26161)",
    version="1.0.0",
)

_JOBS: dict[str, dict] = {}
_WORKERS: dict[str, subprocess.Popen] = {}
_WORKER_LOCK = threading.RLock()
_MAX_WORKERS = 1

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
FRONT_DIR = BASE_DIR / "frontend"


# `_detect_scenario_key_and_name` was deleted (FS-41). It guessed a scenario
# from a run directory name, then from preview bounding boxes, and finally
# returned "phutkal" for anything it could not identify -- labelling an
# unknown run as a real event. It had zero callers; `_rehydrate_saved_scenarios`
# below reads the run manifest, which is the only identification that is
# actually evidence.


def _job_fields_from_result(result: dict) -> dict:
    """The artifact keys a completed job serves, derived from one place.

    Both `_reconcile_job` (live runs) and `_rehydrate_saved_scenarios` (runs
    restored from disk after a restart) need these, and they used to build them
    separately. Rehydration set 10 keys; reconcile set 18; and reconcile
    SHORT-CIRCUITS on a job that is already `done` with a manifest -- which a
    rehydrated job always is. So every key rehydration missed was never filled
    in, for the whole life of the process.

    Measured 2026-09-19 on the archived phutkal run: `/api/roads/{job}` and
    `/api/envelope_geojson/{job}` both 404'd while `latest_job` cheerfully
    reported `has_roads_timeline: true`, because that endpoint reads the
    manifest directly and these read `_JOBS`. `/api/lake_formation` escaped only
    because it has a fallback that looks next to `snapshots_index`.

    The same defect had already been found and patched once, for `hydrograph`
    alone -- see the comment in `_rehydrate_saved_scenarios`. Patching one key
    at a time is why it came back. One map, both callers.
    """
    return {
        "result_path": result.get("results_geojson"),
        # `shp` and `kml` are the ranked SETTLEMENTS layer -- the consequence
        # table. `inundation_shp` / `inundation_kml` are the flood EXTENT
        # itself. A GIS user asking for "the shapefile" of a dam-break study
        # wants the second at least as often as the first, and serving only one
        # of them made the other unreachable even though it was on disk.
        "exports": {"shp": result.get("shp"), "kml": result.get("kml"),
                    "cap": result.get("cap"), "tif": result.get("max_depth_tif"),
                    "inundation_shp": result.get("inundation_shp"),
                    "inundation_kml": result.get("inundation_kml")},
        "snapshots_index": result.get("snapshots_index"),
        "lake_formation": result.get("lake_formation"),
        "validation_agreement": result.get("validation_agreement"),
        "observed_extent": result.get("observed_extent"),
        "validation_roads": result.get("validation_roads"),
        "observed": result.get("observed"),
        "roads_timeline": result.get("roads_timeline"),
        "envelope_geojson": result.get("envelope_geojson"),
        "arrival_time_tif": result.get("arrival_time_tif"),
        "cell_size_m": result.get("cell_size_m"),
        "coarsen": result.get("coarsen"),
        # `/api/run_layer/{job_id}/{kind}` -- backfilled visual layers. Listed
        # here, not just in `_reconcile_job`'s own map, for the same reason
        # `roads_timeline` above is: this dict is the ONE place both callers
        # read from, and a key missing here is missed forever for a job
        # restored from disk after a restart.
        "evac_routes": result.get("evac_routes"),
        "lake_frames": result.get("lake_frames"),
        "water_planes": result.get("water_planes"),
        "front_field": result.get("front_field"),
        "village_depth": result.get("village_depth"),
    }


def _rehydrate_saved_scenarios():
    """Rehydrate only completed, valid manifests; legacy archives stay untrusted."""
    scenarios_dir = DATA_DIR / "scenarios"
    if not scenarios_dir.exists():
        return

    count = 0
    for p in scenarios_dir.iterdir():
        if not p.is_dir():
            continue
        job_id = p.name
        if job_id in _JOBS or not (p / "manifest.json").is_file():
            continue
        try:
            manifest = load_manifest(p / "manifest.json")
            if not is_valid(manifest, run_root=scenarios_dir):
                continue
            result = manifest.get("pipeline_result", {})
            # A rehydrated run had NO hydrograph: `get_hydrograph` returns
            # `job["hydrograph"]`, an in-memory key only the live job path ever
            # set, so every run restored from disk served `{}` and the UI drew an
            # empty chart. The file is on disk and the manifest registers it.
            hydro = {}
            _hp = result.get("hydrograph_json")
            if _hp and Path(_hp).exists():
                try:
                    hydro = json.loads(Path(_hp).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    hydro = {}
            # Every artifact key, from the SAME map `_reconcile_job` uses.
            # Listing a subset here is what made /api/roads and
            # /api/envelope_geojson 404 on every restored run: reconcile
            # short-circuits on an already-`done` job, so whatever is missed
            # here is missed forever.
            _JOBS[job_id] = {"status": "done", "job_id": job_id, "hydrograph": hydro,
                             "manifest": manifest, "scenario_key": manifest.get("scenario_key"),
                             # Without this, `/api/manifest/{job}` called
                             # `load_manifest(None)` and returned 500 for every
                             # restored run -- the endpoint reads it off the job.
                             "manifest_path": str(p / "manifest.json"),
                             "dam_name": manifest.get("request", {}).get("dam_name"),
                             "metrics": manifest.get("metrics", {}),
                             "progress": {"stage": "done", "frac": 1.0},
                             **_job_fields_from_result(result)}
            count += 1
        except ManifestError:
            continue

    logger.info("Rehydrated %d saved simulation scenarios into memory", count)


@app.on_event("startup")
async def startup_event():
    _rehydrate_saved_scenarios()


# Also rehydrate at import time so routes are immediately populated
_rehydrate_saved_scenarios()


class RunRequest(BaseModel):
    # None, not a hardcoded phutkal string. `/api/run` writes `model_dump()`
    # into the run manifest, so a request that omitted `dam_name` recorded
    # "Phutkal River Landslide Dam 2015" as the name of whatever dam it
    # actually ran -- observed 2026-09-19 on a rishiganga job. The CLI has
    # always resolved this from the scenario (`args.dam_name or
    # sc_info.get("name")`); the endpoint now does the same.
    dam_name: Optional[str] = None
    scenario_key: str = "phutkal"
    # REFUSED, and deliberately not removed.
    #
    # This defaulted to 3850.0, and `/api/run` writes `req.model_dump()` into
    # the run manifest, so every archived manifest RECORDS a water level the
    # run never used -- `101520ad...`'s request says 3850.0 against a run that
    # used 3805.11. The pipeline overwrites any supplied level before first
    # use (run_pipeline.py:549 / :552), so the field was pure fiction.
    #
    # Deleting the field would not help: Pydantic ignores unknown keys by
    # default, so a client still sending `wse_m` would have it dropped in
    # silence -- the same no-op, moved to the HTTP boundary. Keeping it with a
    # default of None lets the endpoint tell the caller exactly why it cannot
    # be set, and stops a fictional number entering the manifest.
    wse_m: Optional[float] = None
    failure_mode: str = "overtopping"
    reservoir_level_fraction: float = 0.9
    duration_s: float = 7200.0
    coarsen: int = 4
    custom_dem_path: Optional[str] = None
    crest_length_m: Optional[float] = None
    dam_type: Optional[str] = None
    lulc_raster_path: Optional[str] = None
    population_csv: Optional[str] = None


def _read_progress_sidecar(job: dict) -> dict | None:
    """Progress written by the worker process, if it has written any yet.

    `progress_cb` updates memory in whichever process runs the pipeline, and
    for an API run that is the worker, not this one. `src/api/worker.py` mirrors
    each callback into `progress.json` beside the manifest; this reads it back.
    Returns None rather than raising: a missing or half-written sidecar means
    "no progress to report", never a failed run.
    """
    mp = job.get("manifest_path")
    if not mp:
        return None
    try:
        path = Path(mp).parent / "progress.json"
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _reconcile_job(job_id: str) -> dict | None:
    """Bring _JOBS[job_id] up to date from its manifest before serving it.

    A run's manifest is written by a separate subprocess (src.api.worker), so
    _JOBS (in-memory, this process) can be stale relative to it. This is called
    on every read rather than polled in the background, so there is no separate
    task lifecycle to manage and no risk of a missed poll tick.
    """
    job = _JOBS.get(job_id)
    if not job:
        return None
    if job.get("status") in {"done", "error", "cancelled"} and job.get("manifest"):
        return job  # already reconciled to a terminal state
    manifest_path = job.get("manifest_path")
    if not manifest_path:
        return job
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError:
        return job
    job["manifest"] = manifest
    status = manifest.get("status")
    if status == "completed" and is_valid(manifest, run_root=DATA_DIR / "scenarios"):
        result = manifest.get("pipeline_result", {}) or {}
        job.update({
            "status": "done",
            "hydrograph": _read_json_file(result["hydrograph_json"]) if result.get("hydrograph_json") and Path(result["hydrograph_json"]).exists() else {},
            "metrics": {"total_par": result.get("total_par"), "total_buildings": result.get("total_buildings"), "total_loss_inr": result.get("total_loss_inr")},
            **_job_fields_from_result(result),
        })
    elif status == "completed":
        # Pipeline finished but is_valid() says no (e.g. hash mismatch, missing
        # artifact) — this is a real failure state, not a transient one.
        job["status"] = "error"
        job["error"] = "run completed but failed manifest validation"
    elif status == "failed":
        job["status"] = "error"
        job["error"] = "; ".join(manifest.get("validity", {}).get("reasons", [])) or "run failed"
    elif status in {"cancelled", "interrupted"}:
        job["status"] = "cancelled"
    elif status == "running":
        # Report what the manifest says. This used to fall through to the
        # "leave it as-is" branch below, so a job that had been solving for ten
        # minutes still reported "queued" to the UI.
        job["status"] = "running"
        _p = _read_progress_sidecar(job)
        if _p:
            job["progress"] = _p
    # else the manifest says "queued" — leave _JOBS status as-is, caller polls again
    return job


@app.post("/api/run")
async def run_simulation_endpoint(req: RunRequest):
    from src.scenarios import get_scenario_manifest
    try:
        resolved = get_scenario_manifest(req.scenario_key)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"unknown or unavailable scenario: {exc}")
    if req.wse_m is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                "wse_m is not a settable parameter. The impounded water level is "
                "derived from the scenario's sourced thalweg_m + dam_height_m (or "
                "the cascade reservoir's z_crest_m) and clamped to the sourced "
                "crest_elev_m, so a supplied level is overwritten before first "
                "use. It was silently discarded until 2026-09-18. Omit the field; "
                "the level the run actually used is reported as "
                "validity.initial_wse_m in the run manifest."
            ),
        )
    if not all(map(lambda x: isinstance(x, (int, float)) and x == x and abs(x) != float("inf"), (req.duration_s, req.reservoir_level_fraction))):
        raise HTTPException(status_code=422, detail="numeric request values must be finite")
    if not (0 < req.reservoir_level_fraction <= 1 and 0 < req.duration_s <= 86400 and isinstance(req.coarsen, int) and 1 <= req.coarsen <= 32):
        raise HTTPException(status_code=422, detail="request values out of range")
    if req.failure_mode not in {"overtopping", "piping", "instantaneous", "breach"}:
        raise HTTPException(status_code=422, detail="unsupported failure_mode")
    for value in (req.crest_length_m,):
        if value is not None and (not isinstance(value, (int, float)) or value <= 0 or not (value == value)):
            raise HTTPException(status_code=422, detail="engineering dimensions must be positive finite values")
    analyst_root = (DATA_DIR / "analyst_inputs").resolve()
    for field in ("custom_dem_path", "lulc_raster_path", "population_csv"):
        supplied = getattr(req, field)
        if supplied:
            candidate = Path(supplied).resolve()
            if analyst_root not in candidate.parents or not candidate.is_file():
                raise HTTPException(status_code=422, detail=f"{field} must be an existing file under data/analyst_inputs")
    with _WORKER_LOCK:
        if sum(1 for p in _WORKERS.values() if p.poll() is None) >= _MAX_WORKERS:
            raise HTTPException(status_code=429, detail="simulation worker queue is full")
    job_id = uuid.uuid4().hex
    request_data = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    if not request_data.get("dam_name"):
        # Resolve from the scenario rather than leaving None or a wrong default,
        # so the manifest records the dam that actually ran.
        from src.data_fetcher import SCENARIOS
        request_data["dam_name"] = (SCENARIOS.get(req.scenario_key, {}).get("name")
                                    or f"{req.scenario_key} scenario")
    manifest = new_manifest(req.scenario_key, resolved_scenario=resolved, request=request_data)
    manifest["run_id"] = job_id
    manifest_path = write_manifest(manifest, DATA_DIR / "scenarios")
    # `request_data["dam_name"]`, not `req.dam_name`: the latter is None when the
    # caller omitted it, and the status endpoint would then report the run's dam
    # as null for its whole life. The resolved name is what goes in the manifest.
    _JOBS[job_id] = {"status": "queued", "job_id": job_id,
                     "dam_name": request_data.get("dam_name"),
                     "scenario_key": req.scenario_key, "manifest_path": str(manifest_path)}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen([sys.executable, "-m", "src.api.worker", "--manifest", str(manifest_path)],
                            cwd=str(BASE_DIR), creationflags=creationflags,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with _WORKER_LOCK: _WORKERS[job_id] = proc
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/manifest/{job_id}")
async def get_manifest(job_id: str):
    job = _reconcile_job(job_id)
    # Fall back to the conventional location rather than passing None into
    # `load_manifest`, which raised TypeError and surfaced as a 500.
    path = (job or {}).get("manifest_path") or str(
        DATA_DIR / "scenarios" / job_id / "manifest.json")
    try:
        manifest = load_manifest(path)
    except ManifestError:
        raise HTTPException(status_code=404, detail="Manifest not found")
    # Do not expose local paths or input internals through the API.
    safe = dict(manifest)
    safe.pop("request", None); safe.pop("inputs", None); safe.pop("pipeline_result", None)
    for entry in safe.get("artifacts", {}).values(): entry.pop("path", None)
    return safe


@app.post("/api/cancel/{job_id}")
async def cancel_simulation(job_id: str):
    job = _reconcile_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    with _WORKER_LOCK:
        proc = _WORKERS.get(job_id)
        if proc and proc.poll() is None: proc.terminate()
    try:
        mp = Path(job["manifest_path"])
        manifest = load_manifest(mp)
        if manifest["status"] in {"queued", "running"}:
            transition(manifest, "cancelled"); write_manifest(manifest, DATA_DIR / "scenarios")
    except (KeyError, ManifestError):
        pass
    job["status"] = "cancelled"
    return {"job_id": job_id, "status": "cancelled"}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    job = _reconcile_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    # Expose selected fields; omit large internal paths but include cell_size_m
    # so the frontend can display it in the header badge (D7 fix).
    expose_keys = {"status", "dam_name", "scenario_key", "error",
                   "cell_size_m", "coarsen", "metrics", "progress"}
    return {k: v for k, v in job.items() if k in expose_keys}


@app.get("/api/results/{job_id}")
async def get_results(job_id: str):
    job = _reconcile_job(job_id)
    if not job or job.get("status") != "done" or not job.get("manifest") or not is_valid(job["manifest"], run_root=DATA_DIR / "scenarios"):
        raise HTTPException(status_code=404, detail="Results not ready")
    path = job.get("result_path")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="Result file missing")
    return _read_json_file(path)


@app.get("/api/hydrograph/{job_id}")
async def get_hydrograph(job_id: str):
    job = _reconcile_job(job_id)
    if not job or not job.get("manifest") or not is_valid(job["manifest"], run_root=DATA_DIR / "scenarios"):
        raise HTTPException(status_code=404, detail="Job not found")
    return job.get("hydrograph", {})


@app.get("/api/export/{job_id}/{fmt}")
async def download_export(job_id: str, fmt: str):
    job = _reconcile_job(job_id)
    if not job or job.get("status") != "done" or not job.get("manifest") or not is_valid(job["manifest"], run_root=DATA_DIR / "scenarios"):
        raise HTTPException(status_code=404, detail="Exports not ready")
    exports = job.get("exports", {})
    path = exports.get(fmt)
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail=f"Export format '{fmt}' not found")
    return FileResponse(path, filename=Path(path).name)


@app.get("/api/snapshots/{job_id}")
async def get_snapshots(job_id: str):
    """Return the depth-snapshot frame index for the time-scrubber."""
    job = _reconcile_job(job_id)
    if not job or job.get("status") != "done" or not job.get("manifest") or not is_valid(job["manifest"], run_root=DATA_DIR / "scenarios"):
        raise HTTPException(status_code=404, detail="Snapshots not ready")
    idx_path = job.get("snapshots_index")
    if not idx_path or not Path(idx_path).exists():
        raise HTTPException(status_code=404, detail="Snapshot index missing")
    frames = _read_json_file(idx_path)
    safe_frames = []
    for i, frame in enumerate(frames):
        if not isinstance(frame, dict): continue
        # The pre-breach block carries the reservoir state -- level, inflow,
        # impounded volume, and where the level came from. Those were dropped
        # here, so the stage/discharge chart had nothing to draw and the UI
        # could not say how full the reservoir was at any frame.
        item = {k: frame[k] for k in ("frame_idx", "t_s", "t_min", "event_time_iso", "stage", "phase_title", "preview_bounds", "vector_url", "raster_url", "bytes", "sha256", "provenance", "level_m", "inflow_m3s", "volume_mcm", "area_km2", "level_source") if k in frame}
        item.setdefault("frame_idx", i)
        safe_frames.append(item)
    return {"job_id": job_id, "frame_count": len(safe_frames), "frames": safe_frames}


@app.get("/api/snapshots/{job_id}/frame/{frame_idx}")
async def get_snapshot_frame(job_id: str, frame_idx: int):
    """Return one GeoJSON frame by index."""
    job = _reconcile_job(job_id)
    if not job or job.get("status") != "done" or not job.get("manifest") or not is_valid(job["manifest"], run_root=DATA_DIR / "scenarios"):
        raise HTTPException(status_code=404, detail="Snapshots not ready")
    idx_path = job.get("snapshots_index")
    if not idx_path or not Path(idx_path).exists():
        raise HTTPException(status_code=404, detail="Snapshot index missing")
    frames = _read_json_file(idx_path)
    if frame_idx < 0 or frame_idx >= len(frames):
        raise HTTPException(status_code=404, detail=f"Frame {frame_idx} out of range")
    frame_path = _registered_frame_path(job, frames[frame_idx].get("path", ""))
    return _read_json_file(frame_path)


@app.get("/api/snapshots/{job_id}/frame/{frame_idx}/raster")
async def get_snapshot_raster(job_id: str, frame_idx: int):
    """Return the smooth RGBA preview texture for one real depth frame."""
    job = _reconcile_job(job_id)
    if not job or job.get("status") != "done" or not job.get("manifest") or not is_valid(job["manifest"], run_root=DATA_DIR / "scenarios"):
        raise HTTPException(status_code=404, detail="Snapshots not ready")
    idx_path = job.get("snapshots_index")
    if not idx_path or not Path(idx_path).exists():
        raise HTTPException(status_code=404, detail="Snapshot index missing")
    frames = _read_json_file(idx_path)
    if frame_idx < 0 or frame_idx >= len(frames):
        raise HTTPException(status_code=404, detail=f"Frame {frame_idx} out of range")
    path = frames[frame_idx].get("preview_path")
    if not path:
        raise HTTPException(status_code=404, detail="Snapshot raster missing")
    path = _registered_frame_path(job, path)
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


@app.get("/api/ritter/{job_id}")
async def get_ritter(job_id: str):
    """Return Ritter analytical validation data."""
    job = _reconcile_job(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="Job not ready")
    idx_path = job.get("snapshots_index")
    if not idx_path:
        raise HTTPException(status_code=404, detail="Result path unknown")
    ritter_path = Path(idx_path).parent / "ritter_validation.json"
    if not ritter_path.exists():
        raise HTTPException(status_code=404, detail="Ritter validation file not found")
    return _read_json_file(ritter_path)


@app.get("/api/solver-comparison/{job_id}")
async def get_solver_comparison(job_id: str):
    """Return 1D SWE-SPH vs 2D FV thalweg scenario comparison data."""
    job = _job_or_404(job_id)
    idx_path = job.get("snapshots_index")
    parent = Path(idx_path).parent if idx_path else (Path(job.get("result_path")).parent if job.get("result_path") else None)
    if not parent:
        raise HTTPException(status_code=404, detail="Result path unknown")
    comp_path = parent / "solver_comparison.json"
    if not comp_path.exists():
        raise HTTPException(status_code=404, detail="Solver comparison file not found")
    return _read_json_file(comp_path)


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
    """Return keyed canonical scenario manifests."""
    from src.data_fetcher import SCENARIOS
    from src.scenarios import get_scenario_manifest
    return {key: get_scenario_manifest(key) for key in SCENARIOS}


@app.get("/api/scenarios/{scenario_key}/latest_job")
async def get_latest_scenario_job(scenario_key: str):
    """Return only the latest valid manifest-backed run."""
    manifest = latest_valid(DATA_DIR / "scenarios", scenario_key)
    if not manifest:
        raise HTTPException(status_code=404, detail=f"No completed simulation found for scenario {scenario_key}")
    jid = manifest["run_id"]
    result = manifest.get("pipeline_result", {})
    sn_path = result.get("snapshots_index")
    frame_count = 0
    if sn_path and Path(sn_path).exists():
        try:
            sn_data = _read_json_file(Path(sn_path))
            frame_count = len(sn_data) if isinstance(sn_data, list) else 0
        except Exception:
            pass

    has_lake = bool(result.get("lake_formation") and Path(result["lake_formation"]).exists())
    has_roads = bool(result.get("roads_timeline") and Path(result["roads_timeline"]).exists())

    return {
        "status": "ok",
        "job_id": jid,
        "scenario_key": scenario_key,
        "dam_name": manifest.get("request", {}).get("dam_name"),
        "frame_count": frame_count,
        "has_lake_formation": has_lake,
        "has_roads_timeline": has_roads,
        "metrics": manifest.get("metrics", {}),
    }


@app.get("/api/roads/{job_id}")
async def get_roads_timeline(job_id: str):
    """Return roads_timeline.geojson — per-link cut_time_min for the road status layer."""
    job = _reconcile_job(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="Roads timeline not ready")
    path = job.get("roads_timeline")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="roads_timeline.geojson not found")
    return _read_json_file(path)


# Job keys `_job_fields_from_result` populates for each backfilled layer kind.
_RUN_LAYER_KEYS = {
    "evac_routes": "evac_routes",
    "lake_frames": "lake_frames",
    "water_planes": "water_planes",
    "front_field": "front_field",
    "village_depth": "village_depth",
}


@app.get("/api/run_layer/{job_id}/{kind}")
async def get_run_layer(job_id: str, kind: str):
    """Return a backfilled visual-layer artifact (evac_routes, lake_frames,
    water_planes, front_field, village_depth) for a completed run."""
    if kind not in _RUN_LAYER_KEYS:
        raise HTTPException(status_code=400, detail=f"unknown layer kind: {kind}")
    job = _reconcile_job(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail=f"{kind} not ready")
    path = job.get(_RUN_LAYER_KEYS[kind])
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail=f"{kind} not found for this run")
    return _read_json_file(path)


@app.get("/api/arrival/{job_id}")
async def get_arrival_time_raster(job_id: str):
    """Return the arrival-time GeoTIFF (values in minutes, nodata where never wet)."""
    job = _reconcile_job(job_id)
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
    job = _reconcile_job(job_id)
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
    job = _reconcile_job(job_id)
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
    job = _reconcile_job(job_id)
    if not job or job.get("status") != "done" or not job.get("manifest") or not is_valid(job["manifest"], run_root=DATA_DIR / "scenarios"):
        raise HTTPException(status_code=404, detail="Job not ready")
    return job


def _geojson_or_404(path: Optional[str], what: str) -> dict:
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail=f"{what} not available for this run")
    return _read_json_file(path)


def _registered_frame_path(job: dict, value: str | Path) -> Path:
    """Resolve a frame only when the manifest vouches for it.

    Two ways it can be vouched for. Either the file is itself a registered
    artifact whose content hash the manifest carries, or it is listed inside
    `snapshots_index` -- which IS a registered, hash-verified artifact, so the
    set of paths it names is covered by that same hash. The per-frame GeoJSON
    and preview PNGs are never registered individually (a run has dozens), and
    requiring it made every frame 404 and the flood animation dead in the UI
    while the index itself served fine.

    The index-vouched path is still constrained to the run's own directory, so
    a tampered index cannot be used to read arbitrary files.
    """
    manifest = job.get("manifest")
    if not manifest or not is_valid(manifest, run_root=DATA_DIR / "scenarios"):
        raise HTTPException(status_code=404, detail="Run is not valid")
    candidate = Path(value).resolve()

    for name, entry in (manifest.get("artifacts") or {}).items():
        try:
            resolved = resolve_artifact(manifest, name, run_root=DATA_DIR / "scenarios")
        except ManifestError:
            continue
        if resolved == candidate:
            return resolved

    # Vouched for by the hash-verified snapshot index.
    try:
        idx_path = resolve_artifact(manifest, "snapshots_index",
                                    run_root=DATA_DIR / "scenarios")
    except ManifestError:
        idx_path = None
    if idx_path is not None and idx_path.exists():
        run_dir = idx_path.parent.resolve()
        try:
            candidate.relative_to(run_dir)          # no traversal outside the run
        except ValueError:
            raise HTTPException(status_code=404,
                                detail="Frame artifact is not registered")
        try:
            listed = json.loads(idx_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            listed = []
        for frame in listed:
            if not isinstance(frame, dict):
                continue
            for key in ("path", "preview_path"):
                raw = frame.get(key)
                if not raw:
                    continue
                named = Path(raw)
                if not named.is_absolute():
                    named = run_dir / named
                if named.resolve() == candidate and candidate.exists():
                    return candidate

    raise HTTPException(status_code=404, detail="Frame artifact is not registered")


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


@app.get("/api/validation/{job_id}/arrivals")
async def get_validation_arrivals(job_id: str):
    """Historical arrival time and peak depth validation comparison."""
    job = _job_or_404(job_id)
    idx_path = job.get("snapshots_index")
    if not idx_path:
        raise HTTPException(status_code=404, detail="Result path unknown")
    arr_path = Path(idx_path).parent / "validation_arrivals.json"
    if not arr_path.exists():
        raise HTTPException(status_code=404, detail="Arrival validation file not found")
    return _read_json_file(arr_path)


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
    # Terrain-derived flood corridor (HAND by stage). Deliberately its OWN kind
    # and NOT served through "observed": it is not an observation, and routing it
    # there would relabel terrain analysis as ground truth. The file carries
    # metadata.not_observed and metadata.reason_not_observed; the UI must show
    # them alongside the layer.
    #
    # No longer drawn on the map -- "observed_wse" below replaced it there, because
    # HAND's 2/5/10 m stages are round numbers nobody chose and the terrain is the
    # same DEM the solver runs on. Still served, because scripts/corridor_coverage.py
    # is a legitimate "did the front stall" diagnostic against it.
    "corridor":   ("admin", "{k}_corridor.geojson"),
    # Flood extent whose WATER LEVEL is observed: reported high-water depths from
    # data/observations/<k>/arrivals.json added to the sampled channel bed, then
    # interpolated along the stem and intersected with the DEM. Also its OWN kind
    # and also NOT "observed" -- the level is reported but the shoreline is still
    # our GLO-30, so it is not independent ground truth and carries no CSI.
    # Built by scripts/observed_wse_extent.py.
    "observed_wse": ("admin", "{k}_observed_wse.geojson"),
}


@app.get("/api/layers/{scenario_key}/{kind}")
async def get_context_layer(scenario_key: str, kind: str,
                            start_date: Optional[str] = Query(default=None),
                            end_date: Optional[str] = Query(default=None),
                            bbox: Optional[str] = Query(default=None)):
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
        from src.observation_manifest import validated_observation
        if not (start_date and end_date and bbox):
            return {"type": "FeatureCollection", "features": [], "metadata": {
                "available": False, "scenario_key": scenario_key, "kind": "sar",
                "classification": "NOT_AVAILABLE", "reason": "acquisition dates and bbox are required"}}
        try:
            coords = tuple(float(v.strip()) for v in bbox.split(","))
        except (TypeError, ValueError):
            coords = ()
        return validated_observation(scenario_key, "sar", coords, start_date, end_date)

    if kind not in _LAYER_FILES:
        raise HTTPException(status_code=404, detail=f"Unknown layer '{kind}'")

    subdir, pattern = _LAYER_FILES[kind]
    path = DATA_DIR / subdir / pattern.format(k=scenario_key)
    if not path.exists():
        # Genuinely absent (not every AOI has buildings or facilities). An empty
        # collection is the honest answer: the layer exists and holds nothing —
        # but the caller must be able to tell this apart from a layer that WAS
        # fetched and confirmed empty, so both carry an explicit classification.
        return {
            "type": "FeatureCollection", "features": [],
            "metadata": {"classification": "NOT_AVAILABLE",
                         "reason": f"no {kind} layer has been fetched for this scenario"},
        }
    data = _read_json_file(path)
    n_features = len(data.get("features", [])) if isinstance(data, dict) else 0
    data.setdefault("metadata", {})
    if n_features == 0:
        data["metadata"]["classification"] = "NOT_AVAILABLE"
        data["metadata"]["reason"] = f"{kind} layer was fetched for this scenario and confirmed empty"
    else:
        data["metadata"]["classification"] = "AVAILABLE"
    return data


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
