"""Fail-closed validation for scenario-keyed observed products."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from shapely.geometry import box, mapping, shape

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
OBSERVATION_DIR = DATA_DIR / "observations"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _date(value: str) -> date:
    return date.fromisoformat(value[:10])


def _aoi(manifest: dict[str, Any]):
    aoi = manifest.get("aoi_geojson") or manifest.get("aoi")
    if not aoi:
        raise ValueError("observation manifest lacks AOI")
    if aoi.get("type") == "Feature":
        aoi = aoi["geometry"]
    if aoi.get("type") == "FeatureCollection":
        from shapely.ops import unary_union
        return unary_union([shape(f["geometry"]) for f in aoi.get("features", [])])
    return shape(aoi)


def _safe_metadata(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return API metadata without exposing local cache paths."""
    blocked = {"data_path", "path", "local_path", "cache_path", "raw_path"}
    return {k: v for k, v in manifest.items() if k not in blocked}


def _not_available(scenario_key: str, kind: str, reason: str) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": [], "metadata": {
        "available": False, "scenario_key": scenario_key, "kind": kind,
        "classification": "NOT_AVAILABLE", "reason": reason,
    }}


def validated_observation(scenario_key: str, kind: str, bbox: tuple[float, float, float, float],
                          start_date: str, end_date: str) -> dict[str, Any]:
    """Load one exact scenario/date/AOI product, or return NOT_AVAILABLE."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", scenario_key) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", kind):
        return _not_available(scenario_key, kind, "invalid scenario or observation kind")
    if (len(bbox) != 4 or any(not isinstance(v, (int, float)) for v in bbox) or
            any(not __import__("math").isfinite(float(v)) for v in bbox) or
            not (bbox[0] < bbox[2] and bbox[1] < bbox[3]) or
            bbox[0] < -180 or bbox[2] > 180 or bbox[1] < -90 or bbox[3] > 90):
        return _not_available(scenario_key, kind, "invalid geographic bbox")
    path = OBSERVATION_DIR / scenario_key / f"{kind}.json"
    if not path.exists():
        return _not_available(scenario_key, kind, "no scenario-keyed observation manifest")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        required = {"schema_version", "scenario_key", "kind", "classification", "source_url", "acquisition_start", "acquisition_end", "aoi_geojson", "crs", "data_path", "sha256", "raw_source_hashes"}
        if manifest.get("schema_version") != 1 or not required.issubset(manifest):
            raise ValueError("observation manifest missing required provenance fields")
        if manifest.get("scenario_key") != scenario_key or manifest.get("kind") != kind:
            raise ValueError("scenario or kind mismatch")
        if manifest.get("classification") != "OBSERVED":
            raise ValueError("observation is not classified OBSERVED")
        if not isinstance(manifest["source_url"], str) or not re.fullmatch(r"https?://[^\s]+", manifest["source_url"]):
            raise ValueError("observation source_url must be an absolute HTTP(S) URL")
        if not isinstance(manifest["raw_source_hashes"], (dict, list)) or not manifest["raw_source_hashes"]:
            raise ValueError("observation raw_source_hashes missing")
        aoi = _aoi(manifest)
        if aoi.is_empty or not aoi.is_valid:
            raise ValueError("observation AOI is empty or invalid")
        data_path = (DATA_DIR / manifest["data_path"]).resolve()
        if DATA_DIR.resolve() not in data_path.parents:
            raise ValueError("observation path escapes data directory")
        if not data_path.exists() or sha256_file(data_path) != manifest.get("sha256"):
            raise ValueError("observation hash mismatch or missing data")
        req_start, req_end = _date(start_date), _date(end_date)
        obs_start, obs_end = _date(manifest["acquisition_start"]), _date(manifest["acquisition_end"])
        if req_start > req_end or obs_start > obs_end or req_end < obs_start or req_start > obs_end:
            raise ValueError("requested dates do not overlap acquisition window")
        requested = box(*bbox)
        if not requested.intersects(aoi):
            raise ValueError("requested AOI does not overlap observation AOI")
        data = json.loads(data_path.read_text(encoding="utf-8"))
        # Source products must stay inside their declared AOI. A request is a
        # display subset, so clip intersecting features instead of rejecting a
        # valid product merely because it extends beyond the viewport.
        for feature in data.get("features", []):
            geometry = feature.get("geometry")
            if not geometry:
                raise ValueError("observation feature lacks geometry")
            geom = shape(geometry)
            if geom.is_empty or not geom.is_valid or not aoi.covers(geom):
                raise ValueError("observation geometry is outside declared AOI")
            clipped = geom.intersection(requested)
            feature["geometry"] = mapping(clipped) if not clipped.is_empty else None
        data["features"] = [f for f in data.get("features", []) if f.get("geometry")]
        data["metadata"] = {"available": True, **_safe_metadata(manifest),
                             "requested_bbox": list(bbox), "display_clipped": True}
        return data
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return _not_available(scenario_key, kind, str(exc))
