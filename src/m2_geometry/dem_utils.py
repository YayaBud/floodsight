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

import heapq
import logging
from pathlib import Path

import numpy as np
import rasterio
import rasterio.warp
import rasterio.features
from rasterio import transform as rtransform
from rasterio.crs import CRS
from rasterio.transform import from_bounds
from scipy import ndimage
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
            units = tuple(axis.unit_name for axis in src.crs.axis_info if axis.unit_name)
            if units and any(str(unit).lower() not in {"metre", "meter", "metres", "meters"} for unit in units):
                raise ValueError(f"Custom DEM projected CRS must use metre units, got {units}")
            logger.info("Custom DEM is already projected (%s)", src.crs)
            import shutil
            shutil.copy2(custom_path, out_path)
            return out_path, Provenance.COMPUTED_LIVE

    reprojected = load_and_reproject(custom_path, out_path)
    return reprojected, Provenance.COMPUTED_LIVE


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
    floor_m: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Replace no-data and implausibly low cells with an impermeable wall.

    Reprojecting a mosaic leaves a fringe where bilinear resampling blends real
    elevations toward zero. On the Phutkal tile that fringe is ~1,100 cells in
    the last 24 columns, reading as low as 1 m in terrain whose true floor is
    ~3,580 m. Left alone it is a hole the flood drains into, and every
    downstream depth and arrival time is wrong.

    Cells are flagged when they equal ``nodata``, sit at exactly 0.00 m, or
    fall below the floor. Flagged cells are raised to just above the domain
    maximum so water cannot enter them, which fails safe: the flood is confined
    rather than silently leaking away.

    Two things defeated the naive percentile floor this function used to have,
    and both are handled here:

    * **The artefact sets the statistic.** On the Annamayya tile 279 cells sit
      at exactly 0.00 m, and with them in the array ``percentile(valid, 0.1)``
      *is* 0.00 m -- so the floor landed at -100 m and every one of those cells
      passed as legitimate terrain. 11.38 MCM drained into them in the 24 h run.
      Exact zeros are now flagged before the statistic is taken, so they can
      neither survive nor poison the floor for the near-zero blend cells beside
      them.
    * **A low-lying domain leaves no room for the margin.** Where the valley
      floor is near 100 m, ``percentile - margin_m`` is still below zero and the
      floor protects nothing. A caller that knows the valley-floor elevation
      should pass ``floor_m`` rather than rely on the statistic.

    ``floor_m`` is an absolute elevation in DEM units: cells below it are
    walled. When given it replaces the percentile entirely; the percentile
    stays as the fallback for callers with no sourced value.

    Returns ``(conditioned, mask)`` where ``mask`` is True for repaired cells.
    """
    z = np.asarray(elevation, dtype=np.float64).copy()

    bad = ~np.isfinite(z)
    if nodata is not None:
        bad |= (z == nodata)
    # ponytail: an exact 0.00 m cell in this repo's terrain (Annamayya thalweg
    # 180 m, Phutkal 3,736 m, South Lhonak 5,140 m) is a resampling artefact,
    # never ground. A sea-level domain would need this relaxed -- there is none,
    # and walling fails safe where guessing does not.
    bad |= (z == 0.0)

    valid = z[~bad]
    if valid.size == 0:
        raise ValueError("DEM has no valid cells")

    if floor_m is not None:
        floor = float(floor_m)
        floor_src = "sourced"
    else:
        floor = float(np.percentile(valid, low_percentile)) - margin_m
        floor_src = "p%.2f of non-zero cells - %.0f m" % (low_percentile, margin_m)
    bad |= (z < floor)

    if bad.any():
        wall = float(z[~bad].max()) + margin_m
        z[bad] = wall
        logger.info(
            "DEM conditioned: %d cells (%.2f%%) below %.0f m (%s), exactly zero, "
            "or no-data raised to a %.0f m wall",
            int(bad.sum()), 100.0 * bad.mean(), floor, floor_src, wall,
        )
    return z, bad


def _merged_parts(geoms):
    """Merge line geometries into a list of LineStrings.

    ``linemerge`` raises on a bare LineString in current shapely, which a
    single-reach river produces — found by tests/test_river_pathway.py, and it
    would have taken down any scenario whose OSM waterways merge to one line.
    """
    from shapely.geometry import LineString, MultiLineString
    from shapely.ops import linemerge, unary_union

    union = unary_union(list(geoms))
    if isinstance(union, LineString):
        return [union]
    merged = linemerge(union)
    if isinstance(merged, LineString):
        return [merged]
    if isinstance(merged, MultiLineString):
        return [g for g in merged.geoms if isinstance(g, LineString)]
    return [g for g in getattr(merged, "geoms", []) if isinstance(g, LineString)]


def _cells_to_mask(rows, cols, shape) -> np.ndarray:
    m = np.zeros(shape, dtype=bool)
    m[rows, cols] = True
    return m


def _orthogonalise(rows: np.ndarray, cols: np.ndarray, z: np.ndarray):
    """Insert cells so consecutive path cells share an EDGE, not a corner.

    A finite-volume scheme exchanges flux across cell faces. Two cells that
    touch only at a corner are not connected for it, so a diagonally-stepping
    channel is not a flow path no matter how low its cells are.

    This was not hypothetical. After the flowline was conditioned, the head a
    flood needed to build before any water could reach the domain boundary
    was, measured by a bottleneck (minimax) search from the release cell:

        phutkal    28 m:  8-connected  6.1 m   4-connected 1548.8 m
        rishiganga 28 m:  8-connected 16.3 m   4-connected 1116.1 m

    i.e. the escape route existed only through cell corners and the solver
    could not use a metre of it. Where a diagonal step is unavoidable the
    LOWER of the two orthogonal alternatives is inserted, so the detour never
    raises the path's bottleneck.
    """
    out_r = [int(rows[0])]
    out_c = [int(cols[0])]
    for r, c in zip(rows[1:], cols[1:]):
        r0, c0 = out_r[-1], out_c[-1]
        dr, dc = int(r) - r0, int(c) - c0
        if dr != 0 and dc != 0:
            a = (r0 + np.sign(dr), c0)
            b = (r0, c0 + np.sign(dc))
            pick = a if z[a] <= z[b] else b
            out_r.append(int(pick[0]))
            out_c.append(int(pick[1]))
        out_r.append(int(r))
        out_c.append(int(c))
    return np.asarray(out_r, dtype=int), np.asarray(out_c, dtype=int)


def open_river_outlets(
    elevation: np.ndarray,
    wall_mask: np.ndarray,
    transform,
    river_geoms,
    *,
    source_elev_m: float,
    width_cells: int = 1,
) -> tuple[np.ndarray, dict]:
    """
    Give the domain an outlet where the mapped river leaves the data footprint.

    The problem this solves
    -----------------------
    ``condition_dem`` raises every no-data cell to ``max(DEM) + 100 m``, which
    is right for an interior hole and catastrophic at the boundary: a lat/lon
    AOI reprojected into UTM leaves nodata wedges covering 94-98% of the
    boundary ring, so the river's exit from the AOI is bricked up. Measured
    before this existed — phutkal, 4555/4642 boundary cells walled at 6504 m,
    lowest surviving open cell 4466 m against a 3485 m valley floor, and
    ``volume_outflow_m3`` identically 0.0 in every run. The flood could only
    accumulate; recession and downstream routing were not expressible.

    Why not simply un-wall the whole wedge
    --------------------------------------
    Tried and rejected, measured. Filling every edge-connected nodata cell with
    its nearest valid elevation is terrain that does not exist, and on phutkal
    it invented a passable corridor around the 4-cell barrier: the upstream
    pool and the downstream valley merged into one connected component (1217 +
    55 cells became 1274) and ``validate_geometry`` correctly rejected the
    scenario. Inventing bypass geometry to gain an outlet trades one fabricated
    result for another.

    What this does instead
    ----------------------
    It opens only the cells the **mapped river itself** occupies, and only
    where that river crosses from valid data out to the raster edge. The
    opened cells take the elevation of the last valid river cell before the
    crossing, so the outlet is flat at the bed level the river already had —
    nothing is lowered, no gradient is invented, and the opening is one river
    wide.

    ``source_elev_m`` restricts this to genuine **downstream** exits: an exit
    whose bed sits above the source is not an outlet, and opening it could only
    ever drain the impoundment out of the back of the domain. Leaving those
    walled is hydraulically inert — a transmissive boundary at a cell above the
    water surface passes nothing either way — so the restriction costs no
    physics and removes the bypass risk entirely.

    Returns ``(elevation, report)``.
    """
    z = np.asarray(elevation, dtype=np.float64).copy()
    wall = np.asarray(wall_mask, dtype=bool)
    ny, nx = z.shape
    dx, dy = abs(transform.a), abs(transform.e)
    geoms = [g for g in river_geoms if g is not None and not g.is_empty]
    if not geoms:
        return z, {"available": False, "reason": "no mapped river geometry"}

    parts = _merged_parts(geoms)

    opened = np.zeros(z.shape, dtype=bool)
    outlets: list[dict] = []
    for part in parts:
        n = max(2, int(part.length / (0.5 * max(dx, dy))) + 1)
        pts = np.array([part.interpolate(s).coords[0]
                        for s in np.linspace(0.0, part.length, n)])
        rows, cols = rtransform.rowcol(transform, pts[:, 0], pts[:, 1])
        rows = np.asarray(rows, dtype=int)
        cols = np.asarray(cols, dtype=int)
        inside = (rows >= 0) & (rows < ny) & (cols >= 0) & (cols < nx)
        if not inside.any():
            continue
        rows, cols = rows[inside], cols[inside]
        onwall = wall[rows, cols]
        if not onwall.any() or onwall.all():
            continue
        # Every valid -> walled transition along the line is a candidate exit.
        for direction in (1, -1):
            rr = rows[::direction]
            cc = cols[::direction]
            ww = onwall[::direction]
            k = int(np.argmax(ww)) if ww.any() else -1
            if k <= 0:
                continue
            # Bed elevation at the crossing. Taken as the minimum over the
            # valid cells immediately around the last on-river cell, not that
            # single cell's value: on a 30 m grid the centreline's last sample
            # is as likely to be a bank as the thalweg, and using it directly
            # put rishiganga's outlet at 1770 m when the river's own bed one
            # cell away is 1684 m — an 86 m sill that trapped the flood
            # 1-2 cells short of the boundary it was supposed to leave through.
            ri, ci = int(rr[k - 1]), int(cc[k - 1])
            r0, r1 = max(0, ri - 1), min(ny, ri + 2)
            c0, c1 = max(0, ci - 1), min(nx, ci + 2)
            nb_z = z[r0:r1, c0:c1]
            nb_ok = ~wall[r0:r1, c0:c1]
            z_exit = float(nb_z[nb_ok].min()) if nb_ok.any() else float(z[ri, ci])
            if z_exit > source_elev_m:
                continue          # an uphill exit is not an outlet
            # Does this walled stretch actually reach the raster edge? If it
            # dead-ends inside the domain it is an interior hole, not an exit.
            tail_r, tail_c = rr[k:], cc[k:]
            reaches_edge = bool((tail_r == 0).any() or (tail_r == ny - 1).any()
                                or (tail_c == 0).any() or (tail_c == nx - 1).any())
            if not reaches_edge:
                continue
            tail_r, tail_c = _orthogonalise(tail_r, tail_c, z)
            sel = wall[tail_r, tail_c]
            opened[tail_r[sel], tail_c[sel]] = True
            outlets.append({
                "exit_elev_m": round(z_exit, 2),
                "cells": int(sel.sum()),
                "row": int(tail_r[0]), "col": int(tail_c[0]),
                "_rows": tail_r[sel], "_cols": tail_c[sel], "_z": z_exit,
            })

    if not opened.any():
        return z, {"available": False, "outlets": 0,
                   "reason": "no mapped river crosses the data footprint below the source elevation"}

    # Each outlet's cells take the bed elevation the river already had at the
    # point where it left the valid data — a flat sill at the exit level, not a
    # new gradient. Assigned per outlet rather than by nearest-neighbour lookup,
    # because the nearest non-outlet cell is usually another wall cell and the
    # opening would silently stay at wall height.
    for o in outlets:
        rr, cc = o["_rows"], o["_cols"]
        z[rr, cc] = np.minimum(z[rr, cc], float(o["_z"]))
        if width_cells > 1:
            side = ndimage.binary_dilation(
                _cells_to_mask(rr, cc, z.shape),
                structure=np.ones((3, 3), bool), iterations=width_cells - 1) & wall
            z[side] = np.minimum(z[side], float(o["_z"]))
            opened |= side

    report = {
        "available": True,
        "outlets": len(outlets),
        "cells_opened": int(opened.sum()),
        "source_elev_m": round(float(source_elev_m), 2),
        "outlet_beds_m": sorted({o["exit_elev_m"] for o in outlets}),
    }
    logger.info("M2: %d river outlet(s) opened through the no-data wall, %d cells, "
                "bed elevations %s (source at %.1f m) — the domain can now drain",
                report["outlets"], report["cells_opened"],
                report["outlet_beds_m"], source_elev_m)
    return z, report


def condition_flowline(
    elevation: np.ndarray,
    transform,
    river_lines,
    *,
    protect_mask: np.ndarray | None = None,
    max_drop_m: float = 25.0,
    snap_radius_m: float = 60.0,
) -> tuple[np.ndarray, np.ndarray, dict]:
    """
    Make the mapped river a hydraulically continuous path by removing the
    adverse bed rises a 30 m DSM invents along a gorge floor.

    Why this is needed, measured rather than asserted
    -------------------------------------------------
    Sampled along the OSM reach that passes the breach, on the conditioned DEM
    the solver actually integrates:

        phutkal    28 m: 53/119 steps rise going downstream (44.5%),
                         173 m of total adverse rise against 17 m of net fall,
                         largest downstream sill 30.4 m above the local bed,
                         92% of points need >2 m of ponding to pass the next sill
        rishiganga 28 m: 32/85 adverse (37.6%), largest sill 40.4 m

    A gorge floor 30-60 m wide does not resolve in a 30 m surface model: each
    sampled cell is a blend of channel and valley wall, so the "river" the
    solver sees is a staircase of disconnected bowls. Water must fill each bowl
    to its sill before it can advance, which is exactly the observed failure —
    the flood ponds mid-valley while the real event ran on downstream.

    What this does and does not do
    ------------------------------
    It enforces one physical fact: **a river's bed elevation does not increase
    downstream.** Along the mapped centreline it takes the running minimum and
    lowers only the cells that sit above it. Nothing is lowered below an
    elevation the DEM already reports somewhere upstream on the same reach, no
    channel is created where OSM maps no river, no depth is chosen to make a
    result look right, and the cap below turns a suspicious cut into a refusal
    rather than a canyon.

    ``protect_mask`` (the barrier and the upstream impoundment) is never
    lowered, and the running minimum is restarted on the far side of it — so
    conditioning cannot cut the sill that holds the reservoir back. Without
    that the filter would breach the dam by construction, because the barrier
    is precisely an adverse rise on the flowline.

    Cross-stream snapping comes first, and does most of the work
    -----------------------------------------------------------
    An OSM centreline is accurate to tens of metres and a gorge thalweg is
    narrower than one 30 m cell, so sampling the DEM *on the line* picks up
    valley-wall cells. Most of the apparent staircase is that misregistration,
    not missing terrain. Measured on the phutkal reach at 28 m, taking the
    minimum elevation within a cross-stream window instead of the on-line
    value:

        window   adverse steps   total adverse rise   max downstream sill
        1x1        53/119 (44.5%)      172.8 m              30.4 m
        3x3        25/119 (21.0%)       32.8 m              11.2 m
        5x5        18/119 (15.1%)       15.9 m               4.8 m

    rishiganga at 28 m: 184.6 m -> 35.7 m of adverse rise, sill 40.4 -> 16.2 m.

    So the profile is snapped to the local cross-stream minimum first, using
    only elevations the DEM already reports, and the monotone filter then has
    a small residual to remove. ``snap_radius_m`` defaults to 60 m — roughly
    twice the source posting, which is the scale of OSM-vs-DEM positional
    disagreement — and is a horizontal tolerance, not a depth to tune.

    Returns ``(conditioned_elevation, channel_mask, report)``. ``channel_mask``
    is the set of cells the snapped flowline occupies; roughness should use it
    so the channel's geometry and its Manning n refer to the same cells.
    """
    z = np.asarray(elevation, dtype=np.float64).copy()
    ny, nx = z.shape
    dx = abs(transform.a)
    dy = abs(transform.e)
    cell_area = dx * dy
    protect = (np.zeros(z.shape, dtype=bool) if protect_mask is None
               else np.asarray(protect_mask, dtype=bool))
    if protect.shape != z.shape:
        raise ValueError("protect_mask must match the DEM shape")

    geoms = [g for g in river_lines if g is not None and not g.is_empty]
    channel = np.zeros(z.shape, dtype=bool)
    if not geoms:
        return z, channel, {"available": False, "reason": "no mapped river geometry"}
    parts = [p for p in _merged_parts(geoms) if p.length > 2.0 * dx]

    changed = np.zeros(z.shape, dtype=bool)
    total_drop_m3 = 0.0
    max_drop = 0.0
    rejected = 0
    conditioned_parts = 0
    path_cells = 0
    R = max(1, int(round(snap_radius_m / max(dx, dy))))
    snap_moves: list[float] = []

    for part in parts:
        n = max(2, int(part.length / max(dx, dy)) + 1)
        pts = np.array([part.interpolate(s).coords[0]
                        for s in np.linspace(0.0, part.length, n)])
        rows, cols = rtransform.rowcol(transform, pts[:, 0], pts[:, 1])
        rows = np.asarray(rows, dtype=int)
        cols = np.asarray(cols, dtype=int)
        inside = (rows >= 0) & (rows < ny) & (cols >= 0) & (cols < nx)
        rows, cols = rows[inside], cols[inside]
        if rows.size < 3:
            continue
        # Snap each sample to the lowest cell in a cross-stream window. Only
        # elevations the DEM already reports are used; nothing is invented.
        # CROSS-STREAM only. A square window would also search along the
        # flowline, letting the path skip past a sill instead of resolving it —
        # it would hide the obstruction rather than find the true thalweg. The
        # search runs along the local normal, so it can correct lateral
        # misregistration and nothing else.
        srows = np.empty_like(rows)
        scols = np.empty_like(cols)
        n_s = rows.size
        offs = np.arange(-R, R + 1)
        for m in range(n_s):
            a, b = max(0, m - 1), min(n_s - 1, m + 1)
            t_r, t_c = float(rows[b] - rows[a]), float(cols[b] - cols[a])
            norm = float(np.hypot(t_r, t_c))
            if norm < 1e-9:
                n_r, n_c = 1.0, 0.0
            else:
                n_r, n_c = -t_c / norm, t_r / norm
            cand_r = np.clip(np.rint(rows[m] + offs * n_r).astype(int), 0, ny - 1)
            cand_c = np.clip(np.rint(cols[m] + offs * n_c).astype(int), 0, nx - 1)
            k = int(np.argmin(z[cand_r, cand_c]))
            srows[m], scols[m] = cand_r[k], cand_c[k]
        snap_moves.extend(np.hypot((srows - rows) * dy, (scols - cols) * dx).tolist())
        rows, cols = srows, scols
        # Collapse consecutive repeats so one cell is not filtered against itself.
        keep = np.ones(rows.size, dtype=bool)
        keep[1:] = (rows[1:] != rows[:-1]) | (cols[1:] != cols[:-1])
        rows, cols = rows[keep], cols[keep]
        if rows.size < 3:
            continue
        # The solver exchanges flux across cell FACES, so a corner-to-corner
        # step is not a flow path. See _orthogonalise: without this the
        # conditioned escape route existed only diagonally and the head needed
        # to leave the domain stayed at 1.1-1.5 km.
        rows, cols = _orthogonalise(rows, cols, z)
        channel[rows, cols] = True

        zp = z[rows, cols]
        # Orient downstream. A mapped line carries no flow direction, so the
        # direction is taken from the net fall over the reach's own endpoints
        # rather than assumed from vertex order.
        k = max(1, zp.size // 10)
        if zp[-k:].mean() > zp[:k].mean():
            rows, cols, zp = rows[::-1], cols[::-1], zp[::-1]

        free = ~protect[rows, cols]
        target = zp.copy()
        # Run the monotone filter independently over each contiguous stretch
        # that is not protected, so a protected sill never drags the profile
        # downstream of it.
        i = 0
        while i < free.size:
            if not free[i]:
                i += 1
                continue
            j = i
            while j < free.size and free[j]:
                j += 1
            target[i:j] = np.minimum.accumulate(zp[i:j])
            i = j

        drop = zp - target
        if drop.max() > max_drop_m:
            # A cut this deep is not a DSM staircase, it is the line crossing
            # real ground — an OSM/DEM misregistration, a tributary mapped
            # across a spur, or a reach that leaves the valley. Refuse it.
            rejected += 1
            continue
        touched = drop > 1e-6
        if touched.any():
            z[rows[touched], cols[touched]] = target[touched]
            changed[rows[touched], cols[touched]] = True
            total_drop_m3 += float(drop[touched].sum()) * cell_area
            max_drop = max(max_drop, float(drop.max()))
        conditioned_parts += 1
        path_cells += int(rows.size)

    report = {
        "available": True,
        "reaches_total": len(parts),
        "reaches_conditioned": conditioned_parts,
        "reaches_rejected_too_deep": rejected,
        "max_drop_cap_m": max_drop_m,
        "snap_radius_m": snap_radius_m,
        "snap_radius_cells": R,
        "mean_snap_move_m": round(float(np.mean(snap_moves)), 1) if snap_moves else 0.0,
        "max_snap_move_m": round(float(np.max(snap_moves)), 1) if snap_moves else 0.0,
        "flowline_cells": path_cells,
        "channel_cells": int(channel.sum()),
        "cells_lowered": int(changed.sum()),
        "max_drop_m": round(max_drop, 3),
        "volume_removed_m3": round(total_drop_m3, 1),
        "mean_drop_m": round(total_drop_m3 / cell_area / max(1, int(changed.sum())), 3),
    }
    logger.info(
        "M2: flowline conditioned — %d/%d reaches, snap radius %d cell(s) "
        "(mean move %.0f m), %d of %d flowline cells lowered, max drop %.1f m "
        "(cap %.0f m), %.3e m^3 of terrain removed, %d reaches refused",
        conditioned_parts, len(parts), R, report["mean_snap_move_m"],
        int(changed.sum()), path_cells, max_drop, max_drop_m,
        total_drop_m3, rejected,
    )
    return z, channel, report


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
        # rasterio rowcol uses containing-cell semantics. Rounding shifts
        # points near cell boundaries into a neighbouring channel cell.
        r, c = int(np.floor(row)), int(np.floor(col))
        if 0 <= r < ny and 0 <= c < nx:
            if dem_out[r, c] > max_z:
                logger.info("Thalweg conditioned at (r=%d, c=%d): %.1fm -> %.1fm",
                            r, c, dem_out[r, c], max_z)
                dem_out[r, c] = max_z
                adjusted += 1
                
    return dem_out, adjusted


def escape_head_4connected(z: np.ndarray, row: int, col: int,
                           interface_bed: bool = False,
                           target_mask: np.ndarray | None = None) -> float:
    """Head above the release cell at which it first connects to the DEM edge.

    A bottleneck (minimax) search: the returned value is the lowest water
    surface elevation at which a path exists from ``(row, col)`` to any edge
    cell such that every cell on the path is submerged, moving only across
    cell FACES. Four-connectivity is not a stylistic choice -- it is the
    connectivity a finite-volume scheme actually has. A gap that exists only
    through a cell CORNER carries no flux, so an eight-connected search
    reports an escape route the solver cannot use.

    The *head* is ``escape_head_4connected(...) - z[row, col]``: 0 m means the
    release cell drains straight off the grid, and a large value means water
    must pond that deep before any of it leaves the domain. Used throughout
    Part II (SS26.2, SS30) as the single number describing whether the domain
    is sealed, and by the P1 G3 gate.

    ``interface_bed`` selects what a step across a face costs. The default
    (False) is the terrain question: a path's cost is the highest CELL on it.
    True asks the solver's question instead, and the two can disagree sharply.

    ``_rhs`` treats the bed as continuous piecewise-linear: the bed at an
    interface is the AVERAGE of the two adjoining cell values, so both sides of
    a face see the same bed. That is what makes the scheme well-balanced, and
    it also means a barrier ONE CELL thick does not exist to the solver -- both
    of its faces average it against a lower neighbour, so its effective height
    is halfway down on each side. Measured on a synthetic 28 m grid, a pool
    held 5 m below a 160 m crest: a one-cell wall passes 46.7 % of the pool in
    300 s; a two-cell wall passes 0.000 m^3. With ``interface_bed=True`` the
    cost of stepping from cell a to cell b is ``(z_a + z_b) / 2``, which
    reproduces that result exactly and is therefore the honest test of whether
    an emplaced barrier holds AT THE RESOLUTION BEING RUN.

    ``target_mask`` replaces "the DEM edge" as the destination. Pass the
    downstream side to ask "at what level does this impoundment spill PAST the
    structure", which is the question the barrier-continuity gate needs and is
    not the same as "at what level does water leave the grid".

    Returns ``inf`` if no path exists (unreachable, e.g. all-NaN terrain).
    """
    tgt = None if target_mask is None else np.asarray(target_mask, dtype=bool)
    zz = np.asarray(z, dtype=float)
    ny, nx = zz.shape
    if not (0 <= row < ny and 0 <= col < nx):
        raise ValueError(f"release cell ({row}, {col}) outside the {ny}x{nx} grid")
    start = float(zz[row, col])
    if not np.isfinite(start):
        return float("inf")
    best = np.full(zz.shape, np.inf)
    best[row, col] = start
    heap = [(start, int(row), int(col))]
    while heap:
        lvl, r, c = heapq.heappop(heap)
        if lvl > best[r, c] + 1e-9:
            continue
        if (tgt[r, c] if tgt is not None
                else (r in (0, ny - 1) or c in (0, nx - 1))):
            return float(lvl)
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            i, j = r + dr, c + dc
            if 0 <= i < ny and 0 <= j < nx:
                zij = float(zz[i, j])
                if not np.isfinite(zij):
                    continue
                step = 0.5 * (float(zz[r, c]) + zij) if interface_bed else zij
                nxt = lvl if lvl > step else step
                if nxt < best[i, j] - 1e-9:
                    best[i, j] = nxt
                    heapq.heappush(heap, (nxt, i, j))
    return float("inf")
