"""
M8 — Output Exporters
======================
Generates all PS-required and bonus output artefacts:
  (iii) .shp and .kml  ← PS deliverable, mandatory
  EAP-template-shaped PDF ← maps to NDSA Approved EAP Template (Feb 2026)
  CAP-conformant JSON  ← SACHET-ingestible structure, NOT sent
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# SHP export
# ──────────────────────────────────────────────────────────────────────────────

def _truncate_unique(columns, limit: int = 10) -> tuple[list[str], dict[str, str]]:
    """
    Truncate column names to the Shapefile limit without creating collisions.

    Naive truncation silently merges fields — ``isolation_time_min`` and
    ``isolation_computed`` both become ``isolation_`` — which pyogrio rejects as
    duplicate columns. Colliding names get a numeric suffix instead.

    Returns the new names and a ``{new: original}`` mapping, which the caller
    writes alongside the .shp so the 10-character names can be read back.
    """
    seen: dict[str, int] = {}
    out: list[str] = []
    mapping: dict[str, str] = {}

    for col in columns:
        if col == "geometry":
            out.append(col)
            continue
        base = col[:limit]
        if base not in seen:
            seen[base] = 0
            name = base
        else:
            seen[base] += 1
            suffix = str(seen[base])
            name = base[: limit - len(suffix)] + suffix
        out.append(name)
        if name != col:
            mapping[name] = col
    return out, mapping


def export_shp(gdf: gpd.GeoDataFrame, out_dir: str | Path,
               layer_name: str = "floodsight_results") -> Path:
    """
    Export a GeoDataFrame to a Shapefile.

    Shapefile field names are capped at 10 characters, so a sidecar
    ``<layer>_fields.json`` records the full names for anything truncated.
    """
    out_dir  = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{layer_name}.shp"

    gdf_out = gdf.copy()
    new_cols, mapping = _truncate_unique(gdf_out.columns)
    gdf_out.columns = new_cols

    # Shapefiles have no boolean type; write 0/1 rather than let the driver guess.
    for col in gdf_out.columns:
        if col != "geometry" and gdf_out[col].dtype == bool:
            gdf_out[col] = gdf_out[col].astype(int)

    gdf_out.to_file(str(out_path), driver="ESRI Shapefile")

    if mapping:
        fields_path = out_dir / f"{layer_name}_fields.json"
        with open(fields_path, "w") as f:
            json.dump(mapping, f, indent=2)
        logger.info("Field-name mapping (%d truncated) → %s", len(mapping), fields_path)

    logger.info("Shapefile written → %s", out_path)
    return out_path


# ──────────────────────────────────────────────────────────────────────────────
# KML export
# ──────────────────────────────────────────────────────────────────────────────

def export_kml(gdf: gpd.GeoDataFrame, out_dir: str | Path,
               layer_name: str = "floodsight_results") -> Path:
    """
    Export a GeoDataFrame to a KML file using simplekml.
    Polygons coloured by priority_score (red = high, green = low).
    """
    try:
        import simplekml
    except ImportError:
        raise RuntimeError("simplekml not installed. Run: pip install simplekml")

    out_dir  = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{layer_name}.kml"

    kml = simplekml.Kml(name="FloodSight — Priority Ranking")

    gdf_wgs = gdf.to_crs("EPSG:4326")

    for _, row in gdf_wgs.iterrows():
        score = float(row.get("priority_score", 0.5))
        # AABBGGRR format (KML):
        r = int(255 * score)
        g = int(255 * (1.0 - score))
        b = 0
        colour = simplekml.Color.rgb(r, g, b, a=180)

        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        coords = list(geom.exterior.coords)
        pol    = kml.newpolygon(
            name=str(row.get("village_name", row.get("village_id", ""))),
            outerboundaryis=coords,
        )
        pol.style.polystyle.color   = colour
        pol.style.linestyle.color   = simplekml.Color.black
        pol.style.linestyle.width   = 1

        desc_parts = [
            f"Priority Rank:      #{int(row.get('priority_rank', 0))}",
            f"Priority Score:     {score:.3f}",
            f"Pop at Risk:        {int(row.get('pop_at_risk', 0)):,}",
            f"Isolation Time:     {row.get('isolation_time_min', 'N/A')} min",
            f"Evacuation Window:  {row.get('evacuation_window_min', 'N/A')} min",
            f"Buildings Flooded:  {int(row.get('buildings_flooded', 0))}",
            f"Loss Estimate:      ₹{int(row.get('loss_inr', 0)):,}",
            "",
            "⚠ PROXY DATA: loss estimate uses simplified depth-damage curve.",
        ]
        pol.description = "\n".join(desc_parts)

    kml.save(str(out_path))
    logger.info("KML written → %s", out_path)
    return out_path


# ──────────────────────────────────────────────────────────────────────────────
# CAP JSON
# ──────────────────────────────────────────────────────────────────────────────

def export_cap_json(
    ranked_gdf: gpd.GeoDataFrame,
    dam_name: str,
    breach_params: dict,
    out_dir: str | Path,
    event_time_utc: str | None = None,
) -> Path:
    """
    Generate a CAP-conformant JSON payload.
    This is the structure SACHET would ingest — it is NOT sent.
    """
    out_dir  = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "cap_payload.json"

    now = event_time_utc or datetime.now(timezone.utc).isoformat()

    # Build area list for top-10 priority villages
    areas = []
    for _, row in ranked_gdf.head(10).iterrows():
        geom_wgs = row.geometry
        if geom_wgs is not None and not geom_wgs.is_empty:
            bbox = geom_wgs.bounds
            polygon_str = " ".join(
                f"{lat},{lon}" for lon, lat in geom_wgs.exterior.coords
            )
        else:
            polygon_str = ""

        areas.append({
            "areaDesc": str(row.get("village_name", row.get("village_id", ""))),
            "polygon":  polygon_str,
            "geocode":  [],
            "custom_fields": {
                "priority_rank":       int(row.get("priority_rank", 0)),
                "pop_at_risk":         int(row.get("pop_at_risk", 0)),
                "isolation_time_min":  row.get("isolation_time_min"),
                "evacuation_window_min": row.get("evacuation_window_min"),
            },
        })

    cap = {
        "identifier": f"FloodSight-{dam_name}-{now[:10]}",
        "sender":     "FloodSight-NTRO-SIH2026",
        "sent":        now,
        "status":     "Actual",
        "msgType":    "Alert",
        "scope":      "Restricted",
        "note":       (
            "GENERATED BY FloodSight (SIH26161). "
            "This payload is CAP-structured for SACHET ingestion. "
            "NOT transmitted. Verify with operational authorities before use."
        ),
        "info": [{
            "language":    "en-IN",
            "category":    "Met",
            "event":       "Dam Break / Flash Flood",
            "urgency":     "Immediate",
            "severity":    "Extreme",
            "certainty":   "Likely",
            "effective":   now,
            "headline":    f"FLOOD WARNING — {dam_name} breach scenario",
            "description": (
                f"Hydrodynamic simulation (ANUGA + SWE-SPH) projects inundation "
                f"downstream of {dam_name}. "
                f"Breach params: {breach_params}. "
                f"Top priority village: "
                f"{ranked_gdf.iloc[0].get('village_name', '?')} — "
                f"{int(ranked_gdf.iloc[0].get('pop_at_risk', 0)):,} persons at risk."
            ),
            "area": areas,
        }],
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cap, f, indent=2, ensure_ascii=False, default=str)

    logger.info("CAP JSON written → %s", out_path)
    return out_path
