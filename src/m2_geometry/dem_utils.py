"""
M2 — DEM Utilities
==================
Load and preprocess a FABDEM GeoTIFF for FloodSight.

Steps
-----
1. Open the DEM and reproject to the local UTM zone (all spatial work in metres).
2. Burn a channel polygon (from OSM waterways) by lowering elevation by
   `burn_depth_m` inside the channel — improves low-flow routing on a 30 m DSM.
3. Write the conditioned DEM to a temporary file for use by pysheds and ANUGA.

Why FABDEM?
-----------
FABDEM removes buildings and trees from Copernicus DEM GLO-30 using a
machine-learning canopy/building model. On a 30 m DSM, rooftops and canopy
inflate floodplain depths by 2–5 m. FABDEM gives bare-earth estimates.
Licence: CC BY-NC-SA 4.0. Cite: Hawker et al. (2022), Env. Res. Letters.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import rasterio
import rasterio.warp
import rasterio.features
from rasterio.crs import CRS
from rasterio.transform import from_bounds
from shapely.geometry import mapping, box
import warnings

logger = logging.getLogger(__name__)

_DEFAULT_BURN_DEPTH_M = 3.0   # lower channel by 3 m


def _utm_crs_from_bounds(bounds, src_crs: CRS) -> CRS:
    """Determine the appropriate UTM CRS for the DEM's centre point."""
    from pyproj import Transformer
    transformer = Transformer.from_crs(src_crs, "EPSG:4326", always_xy=True)
    lon, lat = transformer.transform(
        (bounds.left + bounds.right) / 2.0,
        (bounds.bottom + bounds.top) / 2.0,
    )
    zone = int((lon + 180) / 6) + 1
    hemisphere = "north" if lat >= 0 else "south"
    epsg = 32600 + zone if hemisphere == "north" else 32700 + zone
    logger.info("Auto-detected UTM zone %d%s → EPSG:%d", zone,
                "N" if hemisphere == "north" else "S", epsg)
    return CRS.from_epsg(epsg)


def load_and_reproject(dem_path: str | Path, out_path: str | Path) -> Path:
    """
    Reproject DEM to local UTM and write to out_path.

    Returns
    -------
    Path to the reprojected GeoTIFF.
    """
    dem_path = Path(dem_path)
    out_path = Path(out_path)

    with rasterio.open(dem_path) as src:
        utm_crs = _utm_crs_from_bounds(src.bounds, src.crs)

        transform, width, height = rasterio.warp.calculate_default_transform(
            src.crs, utm_crs, src.width, src.height, *src.bounds
        )
        profile = src.profile.copy()
        profile.update(
            crs=utm_crs,
            transform=transform,
            width=width,
            height=height,
            dtype="float32",
            compress="lzw",
            nodata=-9999.0,
        )

        with rasterio.open(out_path, "w", **profile) as dst:
            for i in range(1, src.count + 1):
                rasterio.warp.reproject(
                    source=rasterio.band(src, i),
                    destination=rasterio.band(dst, i),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=utm_crs,
                    resampling=rasterio.warp.Resampling.bilinear,
                )

    logger.info("DEM reprojected to %s → %s", utm_crs, out_path)
    return out_path


def prepare_custom_dem(
    custom_path: str | Path,
    out_dir: str | Path,
):
    """
    Load an arbitrary user-supplied DEM GeoTIFF (e.g. Cartosat, drone LiDAR),
    reproject to optimal local metric UTM CRS if geographic, and return
    (utm_dem_path, Provenance.COMPUTED_LIVE).
    """
    from ..provenance import Provenance
    custom_path = Path(custom_path)
    if not custom_path.exists():
        raise FileNotFoundError(f"Custom DEM not found at {custom_path}")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"custom_{custom_path.stem}_utm.tif"

    with rasterio.open(custom_path) as src:
        if src.crs and src.crs.is_projected:
            logger.info("Custom DEM is already projected (%s)", src.crs)
            import shutil
            shutil.copy2(custom_path, out_path)
            return out_path, Provenance.COMPUTED_LIVE

    reprojected = load_and_reproject(custom_path, out_path)
    return reprojected, Provenance.COMPUTED_LIVE


def burn_channel(
    dem_path: str | Path,
    channel_geometries: list,          # list of Shapely LineString / Polygon geometries
    out_path: str | Path,
    burn_depth_m: float = _DEFAULT_BURN_DEPTH_M,
    buffer_m: float = 30.0,            # half-width of the burned channel
) -> Path:
    """
    Lower the DEM elevation inside a buffered channel polygon.

    Parameters
    ----------
    channel_geometries : OSM waterway LineStrings (in UTM CRS matching the DEM).
    burn_depth_m       : How many metres to lower the channel bed.
    buffer_m           : Half-width to buffer each waterway LineString.
    """
    dem_path = Path(dem_path)
    out_path = Path(out_path)

    with rasterio.open(dem_path) as src:
        assert src.crs.is_projected, "DEM must be in a projected CRS (UTM). Run load_and_reproject first."

        data = src.read(1).astype(np.float32)
        profile = src.profile.copy()
        nodata = src.nodata or -9999.0

        # Build a raster mask for the channel area
        channel_mask = np.zeros(data.shape, dtype=bool)
        for geom in channel_geometries:
            buffered = geom.buffer(buffer_m)
            mask = rasterio.features.geometry_mask(
                [mapping(buffered)],
                out_shape=data.shape,
                transform=src.transform,
                invert=True,
            )
            channel_mask |= mask

        data_valid = data != nodata
        data[channel_mask & data_valid] -= burn_depth_m
        data = np.clip(data, a_min=nodata, a_max=None)

        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(data, 1)

    n_burned = int(channel_mask.sum())
    logger.info("Channel burn complete: %d cells lowered by %.1f m → %s",
                n_burned, burn_depth_m, out_path)
    return out_path


def snap_to_thalweg(
    elevation: np.ndarray,
    transform,
    x: float,
    y: float,
    search_radius_m: float = 2000.0,
) -> tuple[float, float, float, float]:
    """
    Move a point to the lowest cell within ``search_radius_m`` of it.

    Scenario breach coordinates are read off a map, and a coordinate that looked
    right against a smooth synthetic surface can sit high on a valley wall once
    real terrain is loaded — the configured Phutkal breach landed at 5,289 m,
    about 1,400 m above the river it was meant to block. Impounding water there
    produces nothing, and the failure is silent because the simulation still
    runs.

    Snapping to the local minimum puts the breach on the valley floor, which is
    where a landslide dam forms.

    Returns ``(x_snapped, y_snapped, elevation, distance_moved_m)``.
    """
    z = np.asarray(elevation, dtype=float)
    ny, nx = z.shape
    row, col = rasterio.transform.rowcol(transform, x, y)
    row, col = int(row), int(col)

    px = abs(transform.a)
    r = max(1, int(search_radius_m / px))
    r0, r1 = max(0, row - r), min(ny, row + r + 1)
    c0, c1 = max(0, col - r), min(nx, col + r + 1)
    if r0 >= r1 or c0 >= c1:
        raise ValueError(f"Point ({x}, {y}) is outside the DEM")

    window = z[r0:r1, c0:c1]
    wr, wc = np.unravel_index(np.nanargmin(window), window.shape)
    srow, scol = r0 + int(wr), c0 + int(wc)

    sx, sy = rasterio.transform.xy(transform, srow, scol)
    moved = float(np.hypot(sx - x, sy - y))
    logger.info("Breach snapped to the thalweg: moved %.0f m to %.1f m elevation "
                "(was %.1f m)", moved, float(z[srow, scol]), float(z[row, col]))
    return float(sx), float(sy), float(z[srow, scol]), moved


def condition_dem(
    elevation: np.ndarray,
    nodata: float | None = -9999.0,
    low_percentile: float = 0.1,
    margin_m: float = 100.0,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Replace no-data and implausibly low cells with an impermeable wall.

    Reprojecting a mosaic leaves a fringe where bilinear resampling blends real
    elevations toward zero. On the Phutkal tile that fringe is ~1,100 cells in
    the last 24 columns, reading as low as 1 m in terrain whose true floor is
    ~3,580 m. Left alone it is a hole the flood drains into, and every
    downstream depth and arrival time is wrong.

    Cells are flagged when they equal ``nodata`` or fall below
    ``percentile(valid, low_percentile) - margin_m``. Flagged cells are raised
    to just above the domain maximum so water cannot enter them, which fails
    safe: the flood is confined rather than silently leaking away.

    Returns ``(conditioned, mask)`` where ``mask`` is True for repaired cells.
    """
    z = np.asarray(elevation, dtype=np.float64).copy()

    bad = ~np.isfinite(z)
    if nodata is not None:
        bad |= (z == nodata)

    valid = z[~bad]
    if valid.size == 0:
        raise ValueError("DEM has no valid cells")

    floor = float(np.percentile(valid, low_percentile)) - margin_m
    bad |= (z < floor)

    if bad.any():
        wall = float(z[~bad].max()) + margin_m
        z[bad] = wall
        logger.info(
            "DEM conditioned: %d cells (%.2f%%) below %.0f m or no-data raised "
            "to a %.0f m wall", int(bad.sum()), 100.0 * bad.mean(), floor, wall,
        )
    return z, bad


def get_bounds_wgs84(dem_path: str | Path) -> tuple[float, float, float, float]:
    """Return (west, south, east, north) in WGS84 for a (possibly UTM) DEM."""
    with rasterio.open(dem_path) as src:
        bounds = rasterio.warp.transform_bounds(
            src.crs, "EPSG:4326", *src.bounds
        )
    return bounds   # (west, south, east, north)


def compute_hydro_surfaces(
    dem_path: str | Path,
    out_dir: str | Path,
    accumulation_threshold: int = 1000,
) -> tuple[Path, Path]:
    """
    Compute Flow Accumulation and Height Above Nearest Drainage (HAND)
    from a conditioned DEM.
    
    Returns (accumulation_tif, hand_tif)
    """
    try:
        from pysheds.grid import Grid
    except ImportError:
        logger.warning("pysheds not installed. Skipping HAND + Flow Accumulation.")
        return None, None
        
    dem_path = Path(dem_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    
    acc_path = out_dir / f"{dem_path.stem}_acc.tif"
    hand_path = out_dir / f"{dem_path.stem}_hand.tif"
    
    if acc_path.exists() and hand_path.exists():
        logger.info("HAND and accumulation surfaces already exist. Skipping compute.")
        return acc_path, hand_path
        
    logger.info("Computing HAND and Flow Accumulation for %s...", dem_path.name)
    
    # Supress warnings from pysheds about affine types
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        grid = Grid.from_raster(str(dem_path))
        dem = grid.read_raster(str(dem_path))
        
        # 1. Fill depressions
        pit_filled_dem = grid.fill_pits(dem)
        
        # 2. Resolve flats
        flooded_dem = grid.fill_depressions(pit_filled_dem)
        inflated_dem = grid.resolve_flats(flooded_dem)
        
        # 3. Flow direction (D8)
        # using generic directions
        dirmap = (64, 128, 1, 2, 4, 8, 16, 32)
        fdir = grid.flowdir(inflated_dem, dirmap=dirmap)
        
        # 4. Accumulation
        acc = grid.accumulation(fdir, dirmap=dirmap)
        
        # 5. HAND
        drainage_mask = acc > accumulation_threshold
        try:
            hand = grid.compute_hand(fdir, dem, drainage_mask, dirmap=dirmap)
        except AttributeError:
            # Fallback if compute_hand is not in this pysheds version
            logger.warning("pysheds version does not support compute_hand. Skipping HAND.")
            hand = None

    # Write Accumulation
    with rasterio.open(dem_path) as src:
        profile = src.profile.copy()
        
    profile.update(dtype="float32", nodata=-9999.0)
    with rasterio.open(acc_path, "w", **profile) as dst:
        dst.write(acc.astype(np.float32), 1)
        
    # Write HAND
    if hand is not None:
        with rasterio.open(hand_path, "w", **profile) as dst:
            dst.write(hand.astype(np.float32), 1)
    else:
        hand_path = None
            
    logger.info("Hydro surfaces computed: %s, %s", acc_path, hand_path)
    return acc_path, hand_path


def carve_breach_geometry(
    dem: np.ndarray,
    transform: Affine,
    breach_centerline,
    breach_width_m: float = 130.0,
    breach_invert_up_m: float = 185.0,
    breach_invert_down_m: float = 180.0,
    side_slope_hv: float = 1.0,
) -> tuple[np.ndarray, int]:
    """
    Physically carve a 3D breach corridor across an earthen dam embankment.
    
    This function is strictly resolution-independent: the breach axis is
    defined as a Shapely LineString in metric projected coordinates (UTM),
    with an invert floor slope and 1:1 trapezoidal side slopes. The corridor
    is rasterized onto whatever grid resolution (30m, 60m, 120m) is passed.
    
    Parameters
    ----------
    dem : np.ndarray
        2D elevation grid in metres.
    transform : Affine
        Affine transformation matrix mapping raster (col, row) to projected (x, y).
    breach_centerline : LineString
        Centerline of the breach from upstream reservoir to downstream channel.
    breach_width_m : float
        Average top/hydraulic breach width in metres (e.g. 110 - 150 m central).
    breach_invert_up_m : float
        Invert bed elevation at the upstream embankment toe (metres MSL).
    breach_invert_down_m : float
        Invert bed elevation at the downstream channel junction (metres MSL).
    side_slope_hv : float
        Horizontal to vertical ratio of breach side walls (default 1.0 for 1:1).
        
    Returns
    -------
    dem_out : np.ndarray
        Carved DEM array.
    carved_cells : int
        Number of raster cells whose elevation was lowered.
    """
    from shapely.geometry import Point
    
    dem_out = dem.copy()
    ny, nx = dem.shape
    
    buf = breach_width_m * 1.5
    minx, miny, maxx, maxy = breach_centerline.bounds
    minx, maxx = minx - buf, maxx + buf
    miny, maxy = miny - buf, maxy + buf
    
    c_min, r_max = ~transform * (minx, miny)
    c_max, r_min = ~transform * (maxx, maxy)
    
    r0 = max(0, int(min(r_min, r_max)))
    r1 = min(ny, int(max(r_min, r_max)) + 1)
    c0 = max(0, int(min(c_min, c_max)))
    c1 = min(nx, int(max(c_min, c_max)) + 1)
    
    line_len = breach_centerline.length
    half_bottom = max(15.0, (breach_width_m - 20.0) / 2.0)
    
    carved_cells = 0
    for r in range(r0, r1):
        for c in range(c0, c1):
            x, y = transform * (c + 0.5, r + 0.5)
            pt = Point(x, y)
            d = breach_centerline.distance(pt)
            if d > breach_width_m:
                continue
            
            s = breach_centerline.project(pt) / max(1e-3, line_len)
            s = np.clip(s, 0.0, 1.0)
            z_invert = breach_invert_up_m * (1.0 - s) + breach_invert_down_m * s
            
            if d <= half_bottom:
                z_target = z_invert
            else:
                z_target = z_invert + (d - half_bottom) / side_slope_hv
            
            if dem_out[r, c] > z_target:
                dem_out[r, c] = z_target
                carved_cells += 1
                
    logger.info("Breach carved: %d cells lowered along centerline length %.1f m",
                carved_cells, line_len)
    return dem_out, carved_cells


def condition_gorge_thalweg(
    dem: np.ndarray,
    transform: Affine,
    thalweg_points: list[tuple[float, float, float]],
) -> tuple[np.ndarray, int]:
    """
    Ensure narrow gorge thalweg points are not blocked by cell coarsening averaging.
    
    When coarsening a 30m DEM to 120m, a 40m river canyon between 240m cliffs
    can produce a coarsened cell at 190m if 70% of the cell falls on the valley
    wall. This conditions the thalweg to prevent artificial synthetic dams.
    
    Parameters
    ----------
    dem : np.ndarray
        Elevation grid.
    transform : Affine
        Raster affine transform.
    thalweg_points : list of (x, y, max_elevation_m)
        Coordinates in projected metres and maximum allowed thalweg elevation.
    """
    dem_out = dem.copy()
    adjusted = 0
    to_grid = ~transform
    ny, nx = dem.shape
    
    for x, y, max_z in thalweg_points:
        col, row = to_grid * (x, y)
        r, c = int(round(row)), int(round(col))
        if 0 <= r < ny and 0 <= c < nx:
            if dem_out[r, c] > max_z:
                logger.info("Thalweg conditioned at (r=%d, c=%d): %.1fm -> %.1fm",
                            r, c, dem_out[r, c], max_z)
                dem_out[r, c] = max_z
                adjusted += 1
                
    return dem_out, adjusted
