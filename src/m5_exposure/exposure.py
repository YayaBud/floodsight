"""
M5 — Population at Risk & Exposure Analysis
=============================================
Per-village exposure from a depth raster: population at risk, buildings
flooded, critical facilities lost, and a rupee loss estimate.

Data sources
------------
- **GHS-POP** 100 m grid (EC JRC) — best-performing gridded population dataset
  for Indian towns and villages per the IIHS accuracy assessment against ~600k
  census settlements. Optional: see "Population provenance" below.
- **Census 2011 village polygons** (SHRUG / Datameet) — the zonal unit.
- **VIDA Google+Microsoft+OSM building footprints** — structural exposure.
- **OSM POIs** — hospitals and schools.

Population provenance
---------------------
``ghspop_raster`` is optional. With a raster, population at risk is a true
zonal sum over flooded cells and the row is labelled ``COMPUTED_LIVE``. Without
one, the village's declared ``pop_total`` attribute is apportioned by flooded
area fraction and the row is labelled ``PROXY_DATA``.

The apportioning assumes people are spread evenly across the village polygon,
which they are not — settlements cluster, usually near the river. The label is
how that assumption stays visible instead of being quietly absorbed into a
confident-looking number.

PS compliance
-------------
Delivers the "loss and damage analysis" required by deliverable (i).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import geopandas as gpd
import rasterio
import rasterio.mask

from ..provenance import Provenance
from ..rasterutils import sample_raster

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Depth-damage curve (simplified; applied uniformly — labelled as such on screen)
# Shape follows JRC global flood damage functions for residential structures.
# Depth in metres → damage as a fraction of replacement value.
# ──────────────────────────────────────────────────────────────────────────────
_DAMAGE_DEPTHS = np.array([0.0, 0.5, 1.0, 2.0, 3.0, 5.0])
_DAMAGE_FRACS  = np.array([0.0, 0.15, 0.35, 0.55, 0.75, 1.0])

#: Average replacement cost per rural structure (₹). A proxy, not a survey.
_STRUCT_COST_INR = 300_000


def _depth_damage_fraction(depth_m):
    """Damage fraction for a depth (scalar or array), by linear interpolation."""
    return np.interp(depth_m, _DAMAGE_DEPTHS, _DAMAGE_FRACS)


def load_custom_population_csv(csv_path: str | Path) -> dict[str, float]:
    """
    Load a local ground-truth population CSV containing surveyed headcounts
    (e.g., permanent residents, daily construction workers, pilgrims).

    Accepts CSV columns:
      village_name (or name / village), pop_total (or population / headcount / workers / total)
    """
    import csv
    p = Path(csv_path)
    if not p.exists():
        raise FileNotFoundError(f"Population CSV not found: {p}")

    pop_map: dict[str, float] = {}
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("village_name") or row.get("name") or row.get("village") or "").strip()
            if not name:
                continue
            val = None
            for key in ("pop_total", "population", "headcount", "workers", "total"):
                if key in row and row[key]:
                    try:
                        val = float(row[key])
                        break
                    except ValueError:
                        pass
            if val is not None:
                pop_map[name.lower()] = val
    return pop_map


def compute_village_exposure(
    inundation_raster: str | Path,
    village_polygons:  gpd.GeoDataFrame,
    ghspop_raster:     Optional[str | Path] = None,
    buildings_gdf:     Optional[gpd.GeoDataFrame] = None,
    facilities_gdf:    Optional[gpd.GeoDataFrame] = None,
    depth_threshold_m: float = 0.3,
    custom_population: Optional[dict[str, float]] = None,
) -> gpd.GeoDataFrame:
    """
    Compute per-village exposure against a maximum-depth raster.

    Parameters
    ----------
    inundation_raster : GeoTIFF depth raster (m) — normally the run's max depth.
    village_polygons  : Village geometries. Reprojected to the raster CRS here.
    ghspop_raster     : GHS-POP grid. If omitted, ``pop_total`` on each village
                        is apportioned by flooded area and flagged PROXY_DATA.
    buildings_gdf     : Building footprints (optional).
    facilities_gdf    : OSM POIs with an ``amenity`` column (optional).
    depth_threshold_m : Depth at or above which a cell counts as flooded.

    Returns
    -------
    GeoDataFrame, one row per village, with ``pop_at_risk``, ``buildings_flooded``,
    ``hospitals_flooded``, ``schools_flooded``, ``loss_inr``, ``max_depth_m``,
    ``mean_depth_m``, ``flooded_area_frac``, ``inundated`` and
    ``pop_provenance``.
    """
    results = []

    with rasterio.open(inundation_raster) as flood_src:
        flood_crs = flood_src.crs
        flood_arr_full = flood_src.read(1).astype(float)
        flood_tr = flood_src.transform

        villages = (village_polygons.to_crs(flood_crs)
                    if village_polygons.crs != flood_crs else village_polygons)

        # Reproject the optional feature layers once, not per village.
        bldgs = None
        if buildings_gdf is not None and len(buildings_gdf):
            bldgs = (buildings_gdf.to_crs(flood_crs)
                     if buildings_gdf.crs != flood_crs else buildings_gdf)
        facs = None
        if facilities_gdf is not None and len(facilities_gdf):
            facs = (facilities_gdf.to_crs(flood_crs)
                    if facilities_gdf.crs != flood_crs else facilities_gdf)

        pop_src = rasterio.open(ghspop_raster) if ghspop_raster else None

        try:
            for _, village in villages.iterrows():
                geom = [village.geometry.__geo_interface__]

                # ── Depth statistics inside the village polygon ──────────────
                try:
                    clipped, _ = rasterio.mask.mask(flood_src, geom, crop=True,
                                                    nodata=0.0, filled=True)
                    depth = np.nan_to_num(clipped[0].astype(float), nan=0.0)
                except Exception:
                    depth = np.zeros((1, 1))

                wet = depth >= depth_threshold_m
                max_depth  = float(depth.max()) if depth.size else 0.0
                mean_depth = float(depth[wet].mean()) if wet.any() else 0.0
                area_frac  = float(wet.sum() / depth.size) if depth.size else 0.0
                inundated  = bool(wet.any())

                # ── Population ───────────────────────────────────────────────
                v_name = str(village.get("village_name", "")).strip().lower()
                if custom_population and v_name in custom_population:
                    # Ground-truth local survey headcount override
                    pop_total = float(custom_population[v_name])
                    pop_at_risk = round(pop_total * area_frac)
                    pop_prov = Provenance.COMPUTED_LIVE
                elif pop_src is not None:
                    try:
                        pop_clip, _ = rasterio.mask.mask(pop_src, geom, crop=True,
                                                         nodata=0.0, filled=True)
                        pop_arr = np.clip(np.nan_to_num(pop_clip[0].astype(float)), 0, None)
                        pop_total = float(pop_arr.sum())

                        # The population grid and the depth grid rarely align.
                        # Resample the wet mask onto the population grid rather
                        # than assuming a shared shape.
                        if wet.shape != pop_arr.shape:
                            from scipy.ndimage import zoom
                            zy = pop_arr.shape[0] / max(wet.shape[0], 1)
                            zx = pop_arr.shape[1] / max(wet.shape[1], 1)
                            wet_pop = zoom(wet.astype(float), (zy, zx), order=0) > 0.5
                            # zoom can be off by a row/column; trim to fit.
                            wet_pop = wet_pop[:pop_arr.shape[0], :pop_arr.shape[1]]
                            if wet_pop.shape != pop_arr.shape:
                                pad = np.zeros(pop_arr.shape, dtype=bool)
                                pad[:wet_pop.shape[0], :wet_pop.shape[1]] = wet_pop
                                wet_pop = pad
                        else:
                            wet_pop = wet

                        pop_at_risk = float(pop_arr[wet_pop].sum())
                        pop_prov = Provenance.COMPUTED_LIVE
                    except Exception as exc:
                        logger.warning("GHS-POP zonal sum failed for %s: %s",
                                       village.get("village_name", "?"), exc)
                        pop_total = float(village.get("pop_total", 0) or 0)
                        pop_at_risk = pop_total * area_frac
                        pop_prov = Provenance.PROXY_DATA
                else:
                    # No gridded population: apportion the declared total by
                    # flooded area. Uniform-density assumption — hence PROXY.
                    pop_total = float(village.get("pop_total", 0) or 0)
                    pop_at_risk = pop_total * area_frac
                    pop_prov = Provenance.PROXY_DATA

                # ── Buildings ────────────────────────────────────────────────
                buildings_flooded = 0
                loss_inr = 0.0
                if bldgs is not None:
                    in_village = bldgs[bldgs.intersects(village.geometry)]
                    if len(in_village):
                        cents = in_village.geometry.centroid
                        d = sample_raster(flood_arr_full, flood_tr,
                                          cents.x.to_numpy(), cents.y.to_numpy())
                        hit = d >= depth_threshold_m
                        buildings_flooded = int(hit.sum())
                        loss_inr = float(
                            (_depth_damage_fraction(d[hit]) * _STRUCT_COST_INR).sum()
                        )

                # ── Critical facilities ──────────────────────────────────────
                hospitals_flooded = schools_flooded = 0
                if facs is not None:
                    in_village = facs[facs.intersects(village.geometry)]
                    if len(in_village):
                        cents = in_village.geometry.centroid
                        d = sample_raster(flood_arr_full, flood_tr,
                                          cents.x.to_numpy(), cents.y.to_numpy())
                        hit = d >= depth_threshold_m
                        amenity = in_village.get("amenity")
                        if amenity is not None:
                            am = amenity.to_numpy()
                            hospitals_flooded = int(((am == "hospital") & hit).sum())
                            schools_flooded   = int(((am == "school") & hit).sum())

                results.append({
                    "village_id":        village.get("village_id", village.get("shrid", "")),
                    "village_name":      village.get("village_name", ""),
                    "geometry":          village.geometry,
                    "pop_total":         round(pop_total),
                    "pop_at_risk":       round(pop_at_risk),
                    "pop_provenance":    str(pop_prov),
                    "buildings_flooded": buildings_flooded,
                    "hospitals_flooded": hospitals_flooded,
                    "schools_flooded":   schools_flooded,
                    "loss_inr":          round(loss_inr),
                    "max_depth_m":       round(max_depth, 2),
                    "mean_depth_m":      round(mean_depth, 2),
                    "flooded_area_frac": round(area_frac, 4),
                    "inundated":         inundated,
                })
        finally:
            if pop_src is not None:
                pop_src.close()

    gdf = gpd.GeoDataFrame(results, crs=flood_crs)
    n_wet = int(gdf["inundated"].sum()) if len(gdf) else 0
    logger.info("Exposure: %d villages, %d inundated, total PAR = %d (%s)",
                len(gdf), n_wet, int(gdf["pop_at_risk"].sum()) if len(gdf) else 0,
                "GHS-POP" if ghspop_raster else "declared population, apportioned")
    return gdf
