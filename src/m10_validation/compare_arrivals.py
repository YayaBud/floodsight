"""
M10 — Hydrodynamic Arrival Time & Peak Depth Validation
=========================================================
Scores simulated flood wave arrival times and peak water depths against
independently recorded ground truth observations from eye-witness logs,
police incident logs, temple surveys, and disaster records.

Unlike fuzzy string matching, validation points are resolved by strict
geographic coordinates (lat/lon projected to the simulation CRS) and sampled
spatially within a local tolerance radius.
"""

from __future__ import annotations

import json
import logging
import hashlib
import math
import re
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import rasterio
from pyproj import Transformer

logger = logging.getLogger(__name__)

# Ground truth historical arrival observations anchored to evidence records
HISTORICAL_ARRIVALS: dict[str, list[dict[str, Any]]] = {
    "annamayya": [
        {
            "id": "EVD-21",
            "name": "Cheyyeru Gorge Exit",
            "lat": 14.2315,
            "lon": 79.0050,
            "obs_window_ist": "06:15–06:25 AM IST",
            "obs_arrival_min_range": [30.0, 40.0],  # T+30 to T+40 min relative to 05:45 IST overtopping
            "obs_depth_m_range": None,
            "evidence_class": "OBSERVED",
            "source": "EVD-21: Field witness reports / Local police log (~3.0 km gorge transit)",
        },
        {
            "id": "EVD-22",
            "name": "Togurupeta",
            "lat": 14.2539,
            "lon": 79.0433,
            "obs_window_ist": "06:15–06:25 AM IST",
            "obs_arrival_min_range": [30.0, 40.0],
            "obs_depth_m_range": [4.0, 8.0],
            "evidence_class": "OBSERVED",
            "source": "EVD-22: AP Disaster Management / Media Surveys (Indian Express Nov 2021)",
        },
        {
            "id": "EVD-23",
            "name": "Mandapalli",
            "lat": 14.2480,
            "lon": 79.0412,
            "obs_window_ist": "06:25–06:35 AM IST",
            "obs_arrival_min_range": [40.0, 50.0],
            "obs_depth_m_range": [3.5, 6.5],
            "evidence_class": "OBSERVED",
            "source": "EVD-23: Paleswara Swamy Temple survey / Eye-witness logs",
        },
        {
            "id": "EVD-24",
            "name": "Pulapathur",
            "lat": 14.2473,
            "lon": 79.0445,
            "obs_window_ist": "06:35–06:50 AM IST",
            "obs_arrival_min_range": [50.0, 65.0],
            "obs_depth_m_range": None,  # Severe residential collapse (>30 fatalities)
            "evidence_class": "OBSERVED",
            "source": "EVD-24: AP Govt Disaster Management / Casualty Register Kadapa",
        },
        {
            "id": "EVD-25",
            "name": "Gundlur",
            "lat": 14.2522,
            "lon": 79.1140,
            "obs_window_ist": "07:15–07:35 AM IST",
            "obs_arrival_min_range": [90.0, 110.0],
            "obs_depth_m_range": [2.0, 4.0],
            "evidence_class": "OBSERVED",
            "source": "EVD-25: Village Administrative Officer Log / Revenue Records",
        },
        {
            "id": "EVD-26",
            "name": "Nandalur",
            "lat": 14.2580,
            "lon": 79.1200,
            "obs_window_ist": "07:45–08:15 AM IST",
            "obs_arrival_min_range": [120.0, 150.0],
            "obs_depth_m_range": [1.5, 3.5],
            "evidence_class": "OBSERVED",
            "source": "EVD-26: South Central Railway Emergency Bulletin / Track Washout Log",
        },
    ]
}

# Kept above only as an audit trail for the prior implementation. It is not
# eligible as historical truth until each record has a source manifest.
LEGACY_UNVERIFIED_ARRIVALS = HISTORICAL_ARRIVALS
HISTORICAL_ARRIVALS = {}


def _verified_arrivals(scenario_key: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    path = (Path(__file__).resolve().parents[2] / "data" / "observations" /
            scenario_key / "arrivals.json")
    if not path.exists():
        return [], None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        required = {"scenario_key", "classification", "records", "source_hash",
                    "event_clock", "acquisition_proof"}
        if (payload.get("scenario_key") != scenario_key or
                payload.get("classification") != "OBSERVED" or
                not required.issubset(payload) or
                not isinstance(payload["source_hash"], str) or
                not payload["event_clock"] or not payload["acquisition_proof"]):
            return [], None
        if not all(isinstance(r, dict) and isinstance(r.get("source_hash"), str) and
                   len(r["source_hash"]) == 64 for r in payload.get("records", [])):
            return [], None
        records = payload.get("records", [])
        if not isinstance(records, list) or not re.fullmatch(r"[0-9a-fA-F]{64}", payload["source_hash"]):
            return [], None
        return records, {"source_hash": payload["source_hash"], "event_clock": payload["event_clock"], "acquisition_proof": payload["acquisition_proof"]}
    except (OSError, ValueError, TypeError):
        return [], None


def compare_arrivals(
    scenario_key: str,
    max_depth_tif: str | Path,
    raster_stack: Sequence[tuple[float, str | Path]] | None = None,
    out_dir: str | Path | None = None,
    arrival_threshold_m: float = 0.30,
    search_radius_m: float = 300.0,
) -> dict[str, Any]:
    """
    Compare modelled hydrodynamic arrivals against historical ground-truth observations.

    Parameters
    ----------
    scenario_key : str
        Scenario identifier (e.g. "annamayya").
    max_depth_tif : str | Path
        Path to geotiff containing maximum modelled water depth.
    raster_stack : sequence of (t_s, path), optional
        Depth rasters over time used to determine exact arrival time.
    out_dir : str | Path, optional
        Directory where validation_arrivals.json will be saved.
    arrival_threshold_m : float
        Water depth considered arrival (default 0.30 m).
    search_radius_m : float
        Radius in metres around observation coordinates to sample depth/arrival.

    Returns
    -------
    dict
        Structured validation summary with per-location results and overall score.
    """
    records, source_context = _verified_arrivals(scenario_key)
    if not records or source_context is None:
        logger.info("M10: No historical arrival points configured for '%s'", scenario_key)
        res = {
            "available": False,
            "scenario": scenario_key,
            "classification": "NOT_AVAILABLE",
            "reason": f"No ground-truth arrival observations registered for {scenario_key}",
        }
        if out_dir is not None:
            with open(Path(out_dir) / "validation_arrivals.json", "w", encoding="utf-8") as f:
                json.dump(res, f, indent=2)
        return res

    max_depth_tif = Path(max_depth_tif)
    with rasterio.open(max_depth_tif) as src:
        max_depth = src.read(1).astype(float)
        transform = src.transform
        crs = src.crs
        px_w = abs(transform.a)

    if crs is None:
        return {"available": False, "scenario": scenario_key,
                "classification": "NOT_AVAILABLE", "reason": "model raster CRS unavailable"}
    to_proj = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    rad_px = max(1, int(search_radius_m / px_w))

    # Pre-read time-varying stack if provided
    stack_info = []
    if raster_stack:
        for t_s, rpath in raster_stack:
            if t_s >= 0:  # post-breach timesteps
                stack_info.append((float(t_s), Path(rpath)))

    results = []
    matches_count = 0
    depth_matches_count = 0
    total_eval = len(records)

    ny, nx = max_depth.shape

    for rec in records:
        lon, lat = rec["lon"], rec["lat"]
        if not all(isinstance(v, (int, float)) and math.isfinite(float(v)) for v in (lon, lat)):
            continue
        x, y = to_proj.transform(lon, lat)
        row, col = rasterio.transform.rowcol(transform, x, y)

        inside = 0 <= row < ny and 0 <= col < nx

        # Sample maximum depth in local window
        r0 = max(0, row - rad_px)
        r1 = min(ny, row + rad_px + 1)
        c0 = max(0, col - rad_px)
        c1 = min(nx, col + rad_px + 1)

        if inside and r0 < r1 and c0 < c1:
            window = max_depth[r0:r1, c0:c1]
            valid = window[np.isfinite(window) & (window >= 0.0)]
            peak_d = float(np.nanmax(valid)) if valid.size else None
        else:
            peak_d = None

        # Sample arrival time from raster stack
        modeled_arr_min = None
        if stack_info and peak_d is not None and peak_d >= arrival_threshold_m:
            for t_s, rpath in stack_info:
                if not rpath.exists():
                    continue
                with rasterio.open(rpath) as s_src:
                    if s_src.crs != crs or s_src.transform != transform:
                        continue
                    s_arr = s_src.read(1)[r0:r1, c0:c1]
                    s_valid = s_arr[np.isfinite(s_arr)]
                    if s_valid.size and np.nanmax(s_valid) >= arrival_threshold_m:
                        modeled_arr_min = round(t_s / 60.0, 1)
                        break

        # If stack not available or wave arrived, calculate range agreement
        arr_range = rec.get("obs_arrival_min_range")
        arr_status = "NO_TEMPORAL_OUTPUT" if not stack_info else ("NOT_SAMPLED" if not inside else "NO_ARRIVAL")
        signed_err_min = None

        if modeled_arr_min is not None and arr_range:
            lo, hi = arr_range
            if lo <= modeled_arr_min <= hi:
                arr_status = "MATCH"
                signed_err_min = 0.0
                matches_count += 1
            elif modeled_arr_min < lo:
                arr_status = "EARLY"
                signed_err_min = round(modeled_arr_min - lo, 1)
            else:
                arr_status = "LATE"
                signed_err_min = round(modeled_arr_min - hi, 1)

        # Depth range evaluation
        depth_range = rec.get("obs_depth_m_range")
        depth_status = "UNSPECIFIED"
        if depth_range and peak_d is not None:
            d_lo, d_hi = depth_range
            if d_lo <= peak_d <= d_hi:
                depth_status = "IN_RANGE"
                depth_matches_count += 1
            elif peak_d < d_lo:
                depth_status = "LOWER"
            else:
                depth_status = "HIGHER"

        item = {
            "id": rec["id"],
            "name": rec["name"],
            "lat": lat,
            "lon": lon,
            "obs_window_ist": rec["obs_window_ist"],
            "obs_arrival_min_range": arr_range,
            "modeled_arrival_min": modeled_arr_min,
            "arrival_status": arr_status,
            "arrival_signed_error_min": signed_err_min,
            "obs_depth_m_range": depth_range,
            "modeled_peak_depth_m": round(peak_d, 2) if peak_d is not None else None,
            "depth_status": depth_status,
            "evidence_class": rec.get("evidence_class", "OBSERVED"),
            "source": rec["source"],
            "source_hash": rec["source_hash"],
            "observation_context": source_context,
        }
        results.append(item)
        logger.info(
            "M10 Arrival: %-22s Modeled T+%s min (Obs: %s) -> %s | Peak D=%.2f m (%s)",
            rec["name"],
            str(modeled_arr_min),
            rec["obs_window_ist"],
            arr_status,
            peak_d,
            depth_status,
        )

    eligible_arrivals = sum(item["arrival_status"] in {"MATCH", "EARLY", "LATE"} for item in results)
    summary = {
        "available": True,
        "scenario": scenario_key,
        "total_points": total_eval,
        "arrival_matches": matches_count,
        "depth_matches": depth_matches_count,
        "arrival_accuracy_pct": (round(100.0 * matches_count / eligible_arrivals, 1)
                                 if eligible_arrivals else None),
        "arrival_eligible_points": eligible_arrivals,
        "results": results,
        "summary": f"{matches_count} of {total_eval} historical arrival windows matched within tolerance",
    }

    if out_dir is not None:
        out_p = Path(out_dir) / "validation_arrivals.json"
        with open(out_p, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        logger.info("M10: Saved %s", out_p)

    return summary
