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
import math
import zipfile
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
        if base not in seen and base not in out:
            seen[base] = 0
            name = base
        else:
            seen[base] += 1
            suffix = str(seen[base])
            name = base[: limit - len(suffix)] + suffix
            while name in out:
                seen[base] += 1
                suffix = str(seen[base])
                name = base[: limit - len(suffix)] + suffix
        out.append(name)
        if name != col:
            mapping[name] = col
    return out, mapping


def export_shp(gdf: gpd.GeoDataFrame, out_dir: str | Path,
               layer_name: str = "floodsight_results", *, provenance: dict | None = None) -> Path:
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

    # Keep the raw sidecars and offer one unambiguous download artifact.
    zip_path = out_dir / f"{layer_name}.zip"
    sidecars = [out_path.with_suffix(ext) for ext in (".shp", ".shx", ".dbf", ".prj")]
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for sidecar in sidecars:
            if sidecar.exists(): archive.write(sidecar, sidecar.name)
        if mapping and (out_dir / f"{layer_name}_fields.json").exists():
            archive.write(out_dir / f"{layer_name}_fields.json", f"{layer_name}_fields.json")
        if provenance is not None:
            info = out_dir / f"{layer_name}_provenance.json"
            info.write_text(json.dumps(provenance, allow_nan=False, indent=2), encoding="utf-8")
            archive.write(info, info.name)

    logger.info("Shapefile written → %s", out_path)
    return out_path


# ──────────────────────────────────────────────────────────────────────────────
# KML export
# ──────────────────────────────────────────────────────────────────────────────

def _as_int(value, default: int = 0) -> int:
    """Coerce to int, treating None and NaN as absent.

    `row.get(col, 0)` is NOT enough: the column usually EXISTS and holds None,
    so the default never fires and `int(None)` raises. That is what it did —
    `export_kml` crashed on the Annamayya run, whose `buildings_flooded` is None
    because no OSM building layer exists for that AOI.
    """
    if value is None:
        return default
    try:
        if isinstance(value, float) and value != value:   # NaN
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _count_or_note(row, count_col: str, provenance_col: str) -> str:
    """A count, or why there isn't one — never a confident zero for a missing value.

    "Buildings Flooded: 0" and "we never counted the buildings" must not render
    the same way in a file that leaves this system. Where the row carries a
    NOT_COMPUTED provenance, say so instead of printing a number nobody measured.
    """
    value = row.get(count_col)
    if value is None or (isinstance(value, float) and value != value):
        note = row.get(provenance_col)
        if note:
            return f"not computed ({str(note).split(':', 1)[-1].strip()})"
        return "not computed"
    return f"{_as_int(value)}"


def export_kml(gdf: gpd.GeoDataFrame, out_dir: str | Path,
               layer_name: str = "floodsight_results", *, provenance: dict | None = None) -> Path:
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
        raw_score = row.get("priority_score")
        score = float(raw_score) if raw_score is not None and np.isfinite(raw_score) else 0.5
        # AABBGGRR format (KML):
        r = int(255 * score)
        g = int(255 * (1.0 - score))
        b = 0
        colour = simplekml.Color.rgb(r, g, b, a=180)

        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        polygons = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
        for part in polygons:
            pol = kml.newpolygon(name=str(row.get("village_name", row.get("village_id", ""))),
                                 outerboundaryis=list(part.exterior.coords),
                                 innerboundaryis=[list(r.coords) for r in part.interiors])
            pol.style.polystyle.color = colour
            pol.style.linestyle.color = simplekml.Color.black
            pol.style.linestyle.width = 1

        desc_parts = [
            f"Priority Rank:      #{_as_int(row.get('priority_rank'), 0)}",
            f"Priority Score:     {score:.3f}",
            f"Pop at Risk:        {_as_int(row.get('pop_at_risk'), 0):,}",
            f"Isolation Time:     {row.get('isolation_time_min', 'N/A')} min",
            f"Evacuation Window:  {row.get('evacuation_window_min', 'N/A')} min",
            f"Buildings Flooded:  {_count_or_note(row, 'buildings_flooded', 'buildings_provenance')}",
            f"Loss Estimate:      ₹{_as_int(row.get('loss_inr'), 0):,}",
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
    event_time_utc: str | None = None, *, run_id: str | None = None,
    provenance: dict | None = None, certainty: str = "Exercise",
    mode: str = "Exercise",
) -> Path:
    """
    Generate a CAP-conformant JSON payload.
    This is the structure SACHET would ingest — it is NOT sent.

    Raises
    ------
    ValueError
        If ``ranked_gdf`` has no CRS set.
    Exception (from GeoPandas/pyproj)
        If reprojection to EPSG:4326 fails. CAP coordinates must be WGS84;
        this function does not fall back to emitting the input CRS's
        coordinates mislabeled as lat/lon.
    """
    out_dir  = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "cap_payload.json"

    def _clean_num(v):
        """pandas/numpy NaN is not valid JSON; an absent metric is null, not NaN."""
        try:
            if v is None or (isinstance(v, float) and v != v):
                return None
        except TypeError:
            pass
        return v

    if mode != "Exercise":
        raise ValueError("CAP Actual/operational mode requires explicit authorization")
    now = event_time_utc or datetime.now(timezone.utc).isoformat()

    # CAP coordinates are always WGS84, even when the input GeoDataFrame is
    # projected. Reprojection must succeed or the export must fail loudly —
    # silently falling back to `ranked_gdf` as-is on a CRS-less frame or a
    # failed reprojection would emit UTM northing/easting values labelled as
    # lat/lon, pointing an alert at the wrong place.
    if ranked_gdf.crs is None:
        raise ValueError("export_cap_json: ranked_gdf has no CRS set — cannot guarantee WGS84 output")
    ranked_wgs = ranked_gdf.to_crs("EPSG:4326")
    # Build area list for top-10 priority villages
    areas = []
    for _, row in ranked_wgs.head(10).iterrows():
        geom_wgs = row.geometry
        if geom_wgs is not None and not geom_wgs.is_empty:
            bbox = geom_wgs.bounds
            parts = list(geom_wgs.geoms) if geom_wgs.geom_type == "MultiPolygon" else [geom_wgs]
            polygon_str = " ".join(f"{lat},{lon}" for part in parts for lon, lat in part.exterior.coords)
        else:
            polygon_str = ""

        areas.append({
            "areaDesc": str(row.get("village_name", row.get("village_id", ""))),
            "polygon":  polygon_str,
            "geocode":  [],
            "custom_fields": {
                "priority_rank":       int(row.get("priority_rank")) if _clean_num(row.get("priority_rank")) is not None else None,
                "pop_at_risk":         int(row.get("pop_at_risk")) if _clean_num(row.get("pop_at_risk")) is not None else None,
                "isolation_time_min":  _clean_num(row.get("isolation_time_min")),
                "evacuation_window_min": _clean_num(row.get("evacuation_window_min")),
            },
        })

    cap = {
        "identifier": f"FloodSight-{run_id or dam_name}-{now[:10]}",
        "sender":     "FloodSight",
        "sent":        now,
        "status":     "Exercise",
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
            "urgency":     "Unknown",
            "severity":    "Unknown",
            "certainty":   certainty,
            "effective":   now,
            "headline":    f"FLOOD WARNING — {dam_name} breach scenario",
            "description": (
                f"FloodSight exercise output for {dam_name}. "
                f"Executed breach parameters: {breach_params}. "
                f"Top priority village: "
                f"{ranked_wgs.iloc[0].get('village_name', '?') if len(ranked_wgs) else 'no area'} — "
                f"{int(_clean_num(ranked_wgs.iloc[0].get('pop_at_risk')) or 0):,} persons at risk." if len(ranked_wgs) else "No ranked area is available."
            ),
            "area": areas,
        }],
    }

    if provenance is not None:
        cap["provenance"] = provenance
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cap, f, indent=2, ensure_ascii=False, allow_nan=False, default=str)

    logger.info("CAP JSON written → %s", out_path)
    return out_path
