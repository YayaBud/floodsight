"""
M2 — DEM Fill & Stage-Storage Curve
=====================================
Given a conditioned DEM (UTM, bare-earth FABDEM), this module:
1. Finds the impoundment as the water body connected to a seed point.
2. Fills the DEM to the water surface elevation (WSE) to estimate
   impounded volume.
3. Builds the stage-storage curve V(h) and area-elevation curve A(h).
4. Estimates the dam crest elevation and freeboard.

This gives M3 the V_w (volume at failure) needed for Froehlich equations,
and gives the dashboard the visual 3D fill polygon.

Note: 30 m DEM gives ~10–20% volume uncertainty in gorges. State this.
"""

from __future__ import annotations

import logging
import numpy as np
import rasterio
from scipy import ndimage
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# pysheds was imported here for pit filling that the volume calculation never
# used. Connectivity is handled by scipy.ndimage.label instead, which removes an
# install-heavy dependency from the critical path.


@dataclass
class ImpoundmentGeometry:
    """Geometric properties derived from DEM fill analysis."""
    wse_m: float                       # Water Surface Elevation [m, UTM z]
    crest_elev_m: float                # Dam crest (lowest outlet point) [m]
    dam_height_m: float                # WSE - thalweg elevation [m]
    volume_m3: float                   # Impounded volume [m³]
    area_m2: float                     # Water surface area [m²]
    freeboard_m: float                 # crest - WSE (negative if overtopping)
    stage_curve_h: np.ndarray          # stage above thalweg [m]
    stage_curve_V: np.ndarray          # volume at each stage [m³]
    stage_curve_A: np.ndarray          # area  at each stage [m²]
    dem_path: str = ""


def build_stage_storage(
    dem_path: str | Path,
    wse_m: float,
    seed_xy: tuple[float, float],
    outlet_point_xy: Optional[tuple[float, float]] = None,
    barrier_xy: Optional[tuple[float, float]] = None,
    barrier_radius_m: float = 150.0,
    barrier_crest_m: Optional[float] = None,
) -> ImpoundmentGeometry:
    """
    Build the stage-storage curve and impoundment geometry.

    Parameters
    ----------
    dem_path        : conditioned UTM DEM.
    wse_m           : water surface elevation [m] at failure.
    seed_xy         : (x, y) in the DEM's CRS, inside the impoundment. Required.
    outlet_point_xy : optional (x, y) of the outlet; unused for volume, kept for
                      callers that already know the pour point.

    Connectivity
    ------------
    Only the water body **connected to the seed** counts. Selecting every cell
    below the water surface, as this function used to, sweeps in every unrelated
    basin in the raster — a separate valley on the far side of a ridge, three
    kilometres away and hydraulically irrelevant, is counted as impounded water.
    On a Himalayan tile that inflates the volume by a large and silent factor,
    and the volume drives the entire breach hydrograph.

    A seeded flood fill keeps only the pool the dam actually holds back.
    """
    dem_path = Path(dem_path)
    logger.info("Fill analysis on %s (WSE %.1f m)", dem_path, wse_m)

    with rasterio.open(dem_path) as src:
        dem_array = src.read(1).astype(float)
        tr = src.transform
        nodata = src.nodata
        row, col = src.index(seed_xy[0], seed_xy[1])

    if nodata is not None:
        dem_array = np.where(dem_array == nodata, np.inf, dem_array)

    # Put the blockage into the terrain before filling. Without it the pool
    # spreads downstream along the open valley, the rim minimum sits at the
    # water surface, and freeboard is 0 by construction — the fill measures the
    # whole valley system instead of what the dam holds back.
    if barrier_xy is not None:
        brow, bcol = rasterio.transform.rowcol(tr, barrier_xy[0], barrier_xy[1])
        px = abs(tr.a)
        rad = max(1, int(barrier_radius_m / px))
        yy, xx = np.ogrid[:dem_array.shape[0], :dem_array.shape[1]]
        blocked = (yy - int(brow)) ** 2 + (xx - int(bcol)) ** 2 <= rad * rad
        crest = barrier_crest_m if barrier_crest_m is not None else wse_m
        dem_array = np.where(blocked & (dem_array < crest), crest, dem_array)
        logger.info("  blockage emplaced: %d cells raised to %.1f m over a %.0f m radius",
                    int(blocked.sum()), crest, barrier_radius_m)

    ny, nx = dem_array.shape
    if not (0 <= row < ny and 0 <= col < nx):
        raise ValueError(f"Seed {seed_xy} lies outside the DEM extent")

    pix_area_m2 = abs(tr.a * tr.e)

    def connected_pool(level: float) -> np.ndarray:
        """Cells below `level` that are connected to the seed."""
        below = dem_array < level
        if not below[row, col]:
            return np.zeros_like(below)
        labels, _ = ndimage.label(below)          # 4-connectivity
        return labels == labels[row, col]

    water_cells = connected_pool(wse_m)
    if not water_cells.any():
        raise ValueError(
            f"No water body at the seed for WSE {wse_m:.1f} m — the seed sits "
            f"at {dem_array[row, col]:.1f} m, above the water surface."
        )

    z_min = float(dem_array[water_cells].min())
    stages = np.linspace(z_min, wse_m, 100)
    V_arr = np.zeros(100)
    A_arr = np.zeros(100)

    for i, level in enumerate(stages):
        mask = connected_pool(level)
        A_arr[i] = mask.sum() * pix_area_m2
        V_arr[i] = float(np.sum(level - dem_array[mask]) * pix_area_m2) if mask.any() else 0.0

    total_volume = float(V_arr[-1])
    total_area   = float(A_arr[-1])

    # Confinement check. Filling a continuous valley to an arbitrary level
    # always finds a rim cell a few centimetres above the surface, so "lowest
    # point on the rim" is not a useful test. What matters is whether the pool
    # stays inside the domain: a pool touching the raster edge has escaped, and
    # its volume is really the valley system's, not the blockage's.
    edge_touch = (water_cells[0, :].any() or water_cells[-1, :].any() or
                  water_cells[:, 0].any() or water_cells[:, -1].any())
    if edge_touch:
        raise ValueError(
            "the impounded pool reaches the domain edge — it is not confined by "
            "the blockage, so the volume would be the whole valley system's")

    # Freeboard is measured against the blockage that holds the water back, not
    # against the surrounding hillsides.
    if barrier_xy is not None and barrier_crest_m is not None:
        crest_elev = float(barrier_crest_m)
    else:
        expanded  = ndimage.binary_dilation(water_cells)
        perimeter = expanded & ~water_cells & np.isfinite(dem_array)
        crest_elev = float(dem_array[perimeter].min()) if perimeter.any() else wse_m

    dam_height  = float(wse_m - z_min)
    freeboard   = float(crest_elev - wse_m)

    logger.info("  connected pool: %d cells, V = %.3e m^3, A = %.3e m^2, "
                "H = %.1f m, freeboard = %.1f m",
                int(water_cells.sum()), total_volume, total_area,
                dam_height, freeboard)

    return ImpoundmentGeometry(
        wse_m=wse_m,
        crest_elev_m=crest_elev,
        dam_height_m=dam_height,
        volume_m3=total_volume,
        area_m2=total_area,
        freeboard_m=freeboard,
        stage_curve_h=stages - z_min,
        stage_curve_V=V_arr,
        stage_curve_A=A_arr,
        dem_path=str(dem_path),
    )


def compute_lake_depth_grids(
    dem_array: np.ndarray,
    transform,
    seed_xy: tuple[float, float],
    wse_m: float,
    fractions: tuple[float, ...] = (0.25, 0.50, 0.75, 0.95, 1.0),
    barrier_xy: Optional[tuple[float, float]] = None,
    barrier_radius_m: float = 200.0,
    barrier_crest_m: Optional[float] = None,
) -> list[dict]:
    """
    Generate 2D depth grids showing the progressive filling and upstream
    expansion of the natural lake / reservoir behind the river blockage or dam
    prior to breach failure.
    """
    dem_work = dem_array.copy().astype(float)
    if barrier_xy is not None:
        brow, bcol = rasterio.transform.rowcol(transform, barrier_xy[0], barrier_xy[1])
        px = abs(transform.a)
        rad = max(1, int(barrier_radius_m / px))
        yy, xx = np.ogrid[:dem_work.shape[0], :dem_work.shape[1]]
        blocked = (yy - int(brow)) ** 2 + (xx - int(bcol)) ** 2 <= rad * rad
        crest = barrier_crest_m if barrier_crest_m is not None else wse_m
        dem_work = np.where(blocked & (dem_work < crest), crest, dem_work)

    row, col = rasterio.transform.rowcol(transform, seed_xy[0], seed_xy[1])
    ny, nx = dem_work.shape
    row = int(np.clip(row, 0, ny - 1))
    col = int(np.clip(col, 0, nx - 1))

    pix_area_m2 = abs(transform.a * transform.e)

    def connected_pool(level: float) -> np.ndarray:
        below = dem_work < level
        if not below[row, col]:
            return np.zeros_like(below, dtype=bool)
        labels, _ = ndimage.label(below)
        return labels == labels[row, col]

    full_pool = connected_pool(wse_m)
    if not full_pool.any():
        return []

    z_min = float(dem_work[full_pool].min())
    results = []
    for f in fractions:
        level = z_min + float(f) * (wse_m - z_min)
        pool = connected_pool(level)
        if pool.any():
            depth = np.where(pool, np.maximum(0.0, level - dem_work), 0.0).astype(np.float32)
            vol = float(np.sum(depth) * pix_area_m2)
            area = float(pool.sum() * pix_area_m2)
        else:
            depth = np.zeros_like(dem_work, dtype=np.float32)
            vol = 0.0
            area = 0.0
        results.append({
            "fraction": float(f),
            "level_m": float(level),
            "volume_m3": vol,
            "area_m2": area,
            "depth_grid": depth,
        })
    return results
