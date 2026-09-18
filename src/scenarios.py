"""Canonical scenario metadata and geometry access.

The legacy parameter table remains in :mod:`data_fetcher` during migration.
This module is the stable boundary for manifests, provenance, and geometry;
callers must not infer verification from legacy coordinates.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
GEOMETRY_DIR = DATA_DIR / "geometry"
EVIDENCE_DIR = DATA_DIR / "evidence"


def load_event_clock(scenario_key: str) -> dict[str, Any]:
    """Load the server-authored event clock from a scenario's evidence file.

    One clock, one file, one set of timeline_events -- FS-19/§I. A scenario
    without an evidence file (or without an event_clock block in it) reports
    NOT_AVAILABLE rather than a hardcoded fallback origin.
    """
    path = EVIDENCE_DIR / f"{scenario_key}_event_evidence.json"
    empty = {"origin_iso": None, "origin_anchor": None, "classification": "NOT_AVAILABLE",
             "source": None, "uncertainty_s": None, "timeline_events": []}
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    clock = data.get("event_clock")
    if not isinstance(clock, dict) or not clock.get("origin_iso"):
        return empty
    return {
        "origin_iso": clock.get("origin_iso"),
        "origin_anchor": clock.get("origin_anchor"),
        "origin_label": clock.get("origin_label"),
        "classification": clock.get("classification", "RECONSTRUCTED"),
        "source": clock.get("source"),
        "uncertainty_s": clock.get("uncertainty_s"),
        "uncertainty_note": clock.get("uncertainty_note"),
        "timeline_events": _json_safe(clock.get("timeline_events", [])),
    }

INDIAN_EVENT_CATALOG: dict[str, dict[str, Any]] = {
    "wapriyang": {"name": "Wapriyang event", "description": "Indian event record pending source registration"},
    "kosi_2008": {"name": "Kosi flood, 2008", "description": "Indian event record pending source registration"},
    "kashmir_2014": {"name": "Kashmir Valley flood, 2014", "description": "Indian event record pending source registration"},
    "assam_2014": {"name": "Assam flood, 2014", "description": "Indian event record pending source registration"},
}


def _legacy_table() -> dict[str, dict[str, Any]]:
    from .data_fetcher import SCENARIOS as legacy
    return legacy


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "item"):
        return value.item()
    return value


def load_geometry(scenario_key: str) -> dict[str, Any]:
    """Load strict, scenario-keyed geometry; missing geometry stays unavailable."""
    path = GEOMETRY_DIR / f"{scenario_key}.json"
    empty = {"type": "FeatureCollection", "features": []}
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid geometry manifest for '{scenario_key}': {exc}") from exc
    if data.get("schema_version") != 1 or data.get("scenario_key") != scenario_key:
        raise ValueError(f"geometry scenario mismatch: expected '{scenario_key}'")
    fc = data.get("geometry", data)
    if fc.get("type") != "FeatureCollection" or not isinstance(fc.get("features"), list):
        raise ValueError(f"geometry for '{scenario_key}' is not a FeatureCollection")
    valid_roles = {"dam_body", "blockage", "dam_axis", "breach_zone", "spillway", "reservoir", "river", "upstream_seed", "downstream_seed", "breach_point"}
    for feature in fc["features"]:
        if not isinstance(feature, dict):
            raise ValueError(f"geometry for '{scenario_key}' contains a non-feature")
        props = feature.get("properties") or {}
        if props.get("role") not in valid_roles or not props.get("classification") or not props.get("source"):
            raise ValueError(f"geometry feature in '{scenario_key}' lacks role/classification/source")
        geometry = feature.get("geometry") or {}
        if geometry.get("type") not in {"Point", "LineString", "Polygon", "MultiPoint", "MultiLineString", "MultiPolygon"}:
            raise ValueError(f"geometry feature in '{scenario_key}' has invalid geometry")
        coords = json.dumps(geometry.get("coordinates"), allow_nan=False)
        if "NaN" in coords or "Infinity" in coords:
            raise ValueError(f"geometry feature in '{scenario_key}' has non-finite coordinates")
    out = _json_safe(fc)
    # Carry the pre-computed validate_geometry() verdict (P0-3) through, when
    # present -- get_scenario_manifest reads it to set hydraulic_ready rather
    # than a hardcoded literal. Running validate_geometry live would need a
    # loaded DEM on every metadata call; caching the verdict alongside the
    # geometry that produced it keeps that check cheap and honest.
    out["_validated"] = bool(data.get("validated", False))
    out["_validation_reason"] = data.get("validation_reason")
    return out


def _field_provenance(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Classify legacy values without upgrading reconstruction to observation."""
    result = {}
    for key, value in config.items():
        if key in {"name", "description", "event_type"}:
            cls = "RECONSTRUCTED"
        elif key in {"bbox", "utm_epsg", "lat", "lon", "breach_lat", "breach_lon"}:
            cls = "ASSUMED"
        else:
            cls = "RECONSTRUCTED" if value is not None else "NOT_AVAILABLE"
        result[key] = {"classification": cls, "source": "legacy src/data_fetcher.py"}
    return result


def get_scenario_manifest(scenario_key: str) -> dict[str, Any]:
    """Return defensive JSON-safe manifest for one executable or catalog key."""
    legacy = _legacy_table()
    if scenario_key in legacy:
        config = copy.deepcopy(legacy[scenario_key])
        geometry = load_geometry(scenario_key)
        required_roles = {"dam_body", "blockage", "breach_zone", "river", "breach_point",
                          "upstream_seed", "downstream_seed"}
        roles = {f.get("properties", {}).get("role") for f in geometry["features"]}
        has_required_roles = bool(({"dam_body", "blockage"} & roles)) and \
            (required_roles - {"dam_body", "blockage"}).issubset(roles)
        validated = geometry.pop("_validated", False)
        validation_reason = geometry.pop("_validation_reason", None)
        geometry_validation = {
            "schema_version": 1,
            "scenario_key": scenario_key,
            "structural_complete": has_required_roles,
            "validated": bool(validated),
            "roles": sorted(r for r in roles if r),
            "reason": validation_reason if not validated else None,
        }
        reasons = [] if validated else [validation_reason or "verified geometry manifest unavailable"]
        event_clock = load_event_clock(scenario_key)
        event_clock["time_zone"] = "Asia/Kolkata" if event_clock["origin_iso"] else "UTC"
        return {
            "schema_version": 1,
            "scenario_key": scenario_key,
            "name": config.get("name", scenario_key),
            "description": config.get("description", config.get("name", "")),
            "availability": {"hydraulic_ready": bool(geometry_validation["validated"]), "reasons": reasons},
            "defaults": {k: config[k] for k in ("wse_m", "thalweg_m", "utm_epsg", "bbox") if k in config},
            "event_clock": event_clock,
            "geometry": geometry,
            "geometry_validation": geometry_validation,
            "hydrology": copy.deepcopy(config.get("hydrology", config.get("cascade", {}))),
            "model_configuration": copy.deepcopy(config.get("model", config.get("cascade", {}))),
            "inputs": copy.deepcopy(config),
            "validation_sources": [],
            "provenance": {"classification": "RECONSTRUCTED", "source": "legacy src/data_fetcher.py",
                           "field_provenance": _field_provenance(config)},
        }
    if scenario_key in INDIAN_EVENT_CATALOG:
        item = INDIAN_EVENT_CATALOG[scenario_key]
        return {
            "schema_version": 1, "scenario_key": scenario_key,
            "name": item["name"], "description": item["description"],
            "availability": {"hydraulic_ready": False, "reasons": ["event geometry and hydrology sources unavailable"]},
            "defaults": {}, "event_clock": {"origin_iso": None, "time_zone": "UTC", "classification": "NOT_AVAILABLE", "source": None},
            "geometry": {"type": "FeatureCollection", "features": []}, "hydrology": {},
            "model_configuration": {}, "inputs": {}, "validation_sources": [],
            "provenance": {"classification": "NOT_AVAILABLE", "source": None},
        }
    raise KeyError(f"unknown scenario: {scenario_key}")


def __getattr__(name: str):
    if name == "SCENARIOS":
        return _legacy_table()
    raise AttributeError(name)
