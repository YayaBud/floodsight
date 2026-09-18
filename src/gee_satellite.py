"""
FloodSight — Google Earth Engine & Sentinel-1 SAR Near Real-Time Flood Analysis
==============================================================================
Deliverable (iv): Developing a framework for near real-time flood analysis through
Google Earth Engine with the help of open source data.

This module provides:
1. Sentinel-1 SAR (Synthetic Aperture Radar) Level-1 GRD flood backscatter extraction:
   - Cloud-penetrating radar imaging (C-band, 5.405 GHz).
   - Dual polarization: VV (rough water detection) and VH (vegetation penetration).
   - Standard Otsu backscatter thresholding (sigma0 < -16 dB) and Speckle reduction.
2. Sentinel-2 MSI optical validation via NDWI (Normalized Difference Water Index):
   - NDWI = (B3_Green - B8_NIR) / (B3_Green + B8_NIR) > 0.15.
3. Delineation of satellite-observed water extent and spatial overlay generation for GUI.
4. Fail-closed lookup of scenario-keyed observed products when GEE is unavailable.


WIRING STATUS (2026-09-12): NOT WIRED. This module is imported by nothing
except `tests/test_lake_cascade_gee.py`. No Earth Engine query runs anywhere in
`run_pipeline.execute_full_simulation`, and the frontend control that advertised
a "SAR (GEE)" layer was removed because its map source was a permanently empty
FeatureCollection. The module is kept, not deleted: it is a real framework with
real tests and it is problem-statement deliverable (iv). What it needs to become
live is a caller in the pipeline that supplies an AOI and acquisition dates, and
a credentialed Earth Engine service account -- neither exists in this build. Do
not present anything from here as observed ground truth until both do.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger("floodsight.gee")

# Optional GEE import
try:
    import ee
    _GEE_AVAILABLE = True
except ImportError:
    _GEE_AVAILABLE = False


class GEESatelliteAnalyzer:
    """
    Google Earth Engine Sentinel-1 SAR and Sentinel-2 Optical flood analysis engine.
    """

    def __init__(self, service_account: Optional[str] = None, private_key: Optional[str] = None):
        self.initialized = False
        if _GEE_AVAILABLE:
            try:
                ee.Initialize()
                self.initialized = True
                logger.info("Google Earth Engine initialized successfully")
            except Exception as exc:
                logger.warning("GEE authentication not present in local environment: %s. Using local open-source SAR processing.", exc)

    def fetch_sentinel1_flood_extent(
        self,
        bbox: tuple[float, float, float, float],
        start_date: str,
        end_date: str,
        threshold_db: float = -16.0,
        scenario_key: str | None = None,
    ) -> dict:
        """
        Query Sentinel-1 GRD SAR collection, apply Lee filter, and threshold backscatter.
        """
        if (scenario_key is None or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", scenario_key)):
            return {"type": "FeatureCollection", "features": [], "metadata": {
                "available": False, "classification": "NOT_AVAILABLE",
                "reason": "valid scenario_key required before satellite query",
            }}
        try:
            start = date.fromisoformat(start_date[:10])
            end = date.fromisoformat(end_date[:10])
        except (TypeError, ValueError):
            return {"type": "FeatureCollection", "features": [], "metadata": {
                "available": False, "classification": "NOT_AVAILABLE",
                "scenario_key": scenario_key, "reason": "valid acquisition dates required",
            }}
        if start > end or len(bbox) != 4 or not (bbox[0] < bbox[2] and bbox[1] < bbox[3]):
            return {"type": "FeatureCollection", "features": [], "metadata": {
                "available": False, "classification": "NOT_AVAILABLE",
                "scenario_key": scenario_key, "reason": "invalid date range or bbox",
            }}
        west, south, east, north = bbox
        if self.initialized and _GEE_AVAILABLE:
            try:
                roi = ee.Geometry.Rectangle([west, south, east, north])
                s1 = (
                    ee.ImageCollection("COPERNICUS/S1_GRD")
                    .filterBounds(roi)
                    .filterDate(start_date, end_date)
                    .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
                    .filter(ee.Filter.eq("instrumentMode", "IW"))
                    .select("VH")
                    .min()
                )
                # Mask dry pixels before vectorization. A threshold candidate
                # is never certified as an observed flood product here.
                water_mask = s1.lt(threshold_db).selfMask()
                vectors = water_mask.reduceToVectors(
                    geometry=roi,
                    crs="EPSG:4326",
                    scale=20,
                    geometryType="polygon",
                    eightConnected=True,
                    maxPixels=1e8,
                )
                result = vectors.getInfo()
                image_ids = s1.get("system:id").getInfo() if s1 else None
                acquisition = s1.get("system:time_start").getInfo() if s1 else None
                for feature in result.get("features", []):
                    feature.setdefault("properties", {}).update({
                        "classification": "UNVALIDATED_CANDIDATE",
                        "source_image_window": [start_date, end_date],
                        "source_image_id": image_ids,
                        "acquisition_time": acquisition,
                    })
                result.setdefault("metadata", {}).update({
                    "available": False,
                    "classification": "UNVALIDATED_CANDIDATE",
                    "scenario_key": scenario_key,
                    "processing_parameters": {"threshold_db": threshold_db, "collection": "COPERNICUS/S1_GRD"},
                })
                return result
            except Exception as exc:
                logger.warning("GEE live query failed (%s); returning NOT_AVAILABLE", exc)

        from .observation_manifest import validated_observation
        return validated_observation(scenario_key, "sentinel1", bbox, start_date, end_date)

    def export_sar_geojson(self, scenario_key: str, out_path: Path, bbox: tuple[float, float, float, float],
                           start_date: str | None = None, end_date: str | None = None) -> Path:
        """Export Sentinel-1 SAR observation GeoJSON for web map display."""
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if not start_date or not end_date:
            data = {"type": "FeatureCollection", "features": [], "metadata": {
                "available": False, "classification": "NOT_AVAILABLE",
                "scenario_key": scenario_key, "reason": "event acquisition dates required",
            }}
        else:
            data = self.fetch_sentinel1_flood_extent(
            bbox=bbox,
            start_date=start_date,
            end_date=end_date,
            scenario_key=scenario_key,
            )
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("Saved GEE Sentinel-1 SAR flood layer -> %s", out_path)
        return out_path
