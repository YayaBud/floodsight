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
    barrier_mask: Optional[np.ndarray] = None,
    dem_array: Optional[np.ndarray] = None,
    transform: Optional[object] = None,
) -> ImpoundmentGeometry:
    """
    Build the stage-storage curve and impoundment geometry.

    Parameters
    ----------
    dem_path        : conditioned UTM DEM. Used to read the array from disk
                      UNLESS `dem_array`/`transform` are both supplied.
    wse_m           : water surface elevation [m] at failure.
    seed_xy         : (x, y) in the DEM's CRS, inside the impoundment. Required.
    outlet_point_xy : optional (x, y) of the outlet; unused for volume, kept for
                      callers that already know the pour point.
    dem_array, transform : pass the CALLER's already-loaded (and possibly
                      coarsened) DEM array + affine transform instead of
                      re-reading `dem_path` from disk at full resolution.
                      Required whenever `barrier_mask` was rasterized against
                      a coarsened grid (`run_pipeline.py`'s `coarsen` support)
                      — reading a fresh full-resolution array from `dem_path`
                      here would silently mismatch that mask's shape. Both
                      must be given together, or neither.

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

    if (dem_array is None) != (transform is None):
        raise ValueError("dem_array and transform must be supplied together, or neither")

    if dem_array is not None:
        # Already conditioned by the caller (run_pipeline.py runs every DEM
        # through condition_dem before this point), so nodata is already
        # resolved — no separate nodata pass needed here.
        dem_array = np.asarray(dem_array, dtype=float).copy()
        tr = transform
        nodata = None
        row, col = rasterio.transform.rowcol(tr, seed_xy[0], seed_xy[1])
    else:
        with rasterio.open(dem_path) as src:
            if src.crs is None or not src.crs.is_projected:
                raise ValueError("stage-storage DEM must use a projected CRS with metre units")
            dem_array = src.read(1).astype(float)
            tr = src.transform
            nodata = src.nodata
            row, col = src.index(seed_xy[0], seed_xy[1])

    ny, nx = dem_array.shape
    if not (0 <= row < ny and 0 <= col < nx):
        raise ValueError(f"Seed {seed_xy} lies outside the DEM extent")
    if nodata is not None:
        dem_array = np.where(dem_array == nodata, np.inf, dem_array)

    # Put the blockage into the terrain before filling. Without it the pool
    # spreads downstream along the open valley, the rim minimum sits at the
    # water surface, and freeboard is 0 by construction — the fill measures the
    # whole valley system instead of what the dam holds back.
    if barrier_mask is not None:
        if np.asarray(barrier_mask).shape != dem_array.shape:
            raise ValueError("barrier_mask must match DEM shape")
        if barrier_crest_m is None or not np.isfinite(barrier_crest_m):
            raise ValueError("barrier_crest_m required with barrier_mask")
        dem_array = np.where(np.asarray(barrier_mask, dtype=bool) & (dem_array < barrier_crest_m), barrier_crest_m, dem_array)
    elif barrier_xy is not None:
        brow, bcol = rasterio.transform.rowcol(tr, barrier_xy[0], barrier_xy[1])
        px = abs(tr.a)
        rad = max(1, int(barrier_radius_m / px))
        yy, xx = np.ogrid[:dem_array.shape[0], :dem_array.shape[1]]
        blocked = (yy - int(brow)) ** 2 + (xx - int(bcol)) ** 2 <= rad * rad
        crest = barrier_crest_m if barrier_crest_m is not None else wse_m
        dem_array = np.where(blocked & (dem_array < crest), crest, dem_array)
        logger.info("  blockage emplaced: %d cells raised to %.1f m over a %.0f m radius",
                    int(blocked.sum()), crest, barrier_radius_m)

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

    # The seed's connected component can be empty at levels only fractionally
    # above z_min (the pool's own lowest cell, per construction, generally
    # sits AT z_min, not strictly below it, so `connected_pool` returns
    # nothing until `level` climbs past it) -- this produces a run of leading
    # V=0 duplicate stages. A stage-storage curve consumers rely on (e.g.
    # m3_breach.ensemble.route_breach) must be STRICTLY increasing to invert
    # (depth as a function of remaining volume); collapse the flat run to its
    # single true floor point rather than passing duplicate zeros through and
    # having every downstream consumer defend against it separately.
    first_positive = int(np.argmax(V_arr > 0.0)) if np.any(V_arr > 0.0) else 0
    if first_positive > 1:
        keep = np.concatenate(([first_positive - 1], np.arange(first_positive, len(stages))))
        stages, V_arr, A_arr = stages[keep], V_arr[keep], A_arr[keep]

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
    levels_m: Optional[list[float]] = None,
    barrier_mask: Optional[np.ndarray] = None,
) -> list[dict]:
    """
    Generate 2D depth grids showing the progressive filling and upstream
    expansion of the natural lake / reservoir behind the river blockage or dam
    prior to breach failure.

    Parameters
    ----------
    levels_m : list[float] or None
        When provided, use these absolute water surface elevations directly
        instead of deriving them from ``fractions`` of ``wse_m``.  This allows
        pre-breach frames to use physically computed elevations from
        ``simulate_prebreach_rise`` rather than generic fractions.
    """
    dem_work = dem_array.copy().astype(float)
    if barrier_mask is not None:
        if np.asarray(barrier_mask).shape != dem_work.shape:
            raise ValueError("barrier_mask must match DEM shape")
        if barrier_crest_m is None or not np.isfinite(barrier_crest_m):
            raise ValueError("barrier_crest_m required with barrier_mask")
        dem_work = np.where(np.asarray(barrier_mask, dtype=bool) & (dem_work < barrier_crest_m), barrier_crest_m, dem_work)
    elif barrier_xy is not None:
        brow, bcol = rasterio.transform.rowcol(transform, barrier_xy[0], barrier_xy[1])
        px = abs(transform.a)
        rad = max(1, int(barrier_radius_m / px))
        yy, xx = np.ogrid[:dem_work.shape[0], :dem_work.shape[1]]
        blocked = (yy - int(brow)) ** 2 + (xx - int(bcol)) ** 2 <= rad * rad
        crest = barrier_crest_m if barrier_crest_m is not None else wse_m
        dem_work = np.where(blocked & (dem_work < crest), crest, dem_work)

    row, col = rasterio.transform.rowcol(transform, seed_xy[0], seed_xy[1])
    ny, nx = dem_work.shape
    if not (0 <= row < ny and 0 <= col < nx):
        raise ValueError("seed lies outside DEM extent")

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

    # When levels_m is provided, use absolute elevations directly.
    # Otherwise derive from fractions of (wse_m - z_min).
    if levels_m is not None:
        levels = np.asarray(levels_m, dtype=float)
        if levels.ndim != 1 or levels.size == 0 or not np.all(np.isfinite(levels)) or np.any(np.diff(levels) < 0):
            raise ValueError("levels_m must be finite and nondecreasing")
        if float(levels[0]) < z_min or float(levels[-1]) > wse_m:
            raise ValueError("levels_m must lie within connected pool stage range")
        results = []
        for level in levels:
            frac = (level - z_min) / (wse_m - z_min) if wse_m > z_min else 0.0
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
                "fraction": float(np.clip(frac, 0.0, 1.0)),
                "level_m": float(level),
                "volume_m3": vol,
                "area_m2": area,
                "depth_grid": depth,
            })
        return results

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

def _pool_machinery(dem_array: np.ndarray, transform, seed_xy: tuple[float, float],
                    barrier_mask: Optional[np.ndarray] = None,
                    barrier_crest_m: Optional[float] = None):
    """Barrier-emplaced grid, seed cell, pixel area and a connected-pool probe.

    Built ONCE and reused, so a 240-point probe sweep does not rebuild the
    working array 240 times. The barrier emplacement here is the same three
    lines `compute_lake_depth_grids` performs internally;
    `test_lake_formation_levels.py::test_probe_agrees_with_compute_lake_depth_grids`
    pins the two together so this copy cannot drift.
    """
    dem_work = dem_array.copy().astype(float)
    if barrier_mask is not None:
        if np.asarray(barrier_mask).shape != dem_work.shape:
            raise ValueError("barrier_mask must match DEM shape")
        if barrier_crest_m is None or not np.isfinite(barrier_crest_m):
            raise ValueError("barrier_crest_m required with barrier_mask")
        dem_work = np.where(
            np.asarray(barrier_mask, dtype=bool) & (dem_work < barrier_crest_m),
            barrier_crest_m, dem_work)

    row, col = rasterio.transform.rowcol(transform, seed_xy[0], seed_xy[1])
    row, col = int(row), int(col)
    ny, nx = dem_work.shape
    if not (0 <= row < ny and 0 <= col < nx):
        raise ValueError("seed lies outside DEM extent")
    pix_area_m2 = abs(transform.a * transform.e)

    def volume_at(level: float) -> float:
        below = dem_work < level
        if not below[row, col]:
            return 0.0
        labels, _ = ndimage.label(below)
        pool = labels == labels[row, col]
        return float(np.sum(np.where(pool, level - dem_work, 0.0)) * pix_area_m2)

    return dem_work, (row, col), pix_area_m2, volume_at


def equal_volume_levels(vol_of_level, z_lo: float, z_hi: float, n_frames: int,
                        n_probe: int = 240) -> tuple[list[float], list[dict]]:
    """Levels whose impounded volumes are as equally spaced as terrain allows.

    Equal water per frame rather than equal height per frame. On a gorge the
    difference is the whole animation: measured on phutkal, 3745->3765 m is
    29 % of the height range and holds **12.9 %** of the water, while
    3785->3805 m is the same 29 % and holds **47 %**. Spacing by stage gives
    frames that are empty, empty, puddle, lake.

    V(z) is NOT continuous for a real seeded fill. Measured on phutkal: one
    0.29 m probe step at **3763.05 m adds 2.488 MCM** against a ~0.24 typical --
    the pool tops a sill and swallows an adjacent basin in one step. No level
    exists whose volume lands inside that gap, so a strict equal-volume target
    is unsatisfiable there and several targets collapse onto the same level.
    Levels are deduplicated afterwards, and the discontinuities are RETURNED so
    the caller reports them instead of smoothing them away -- a lake capturing a
    side valley is a real feature, not noise.

    `vol_of_level` is a callable so both backends share this: a measured seeded
    fill for the DEM backend, an analytical curve for the hypsometric one.
    """
    probe = np.linspace(z_lo, z_hi, n_probe)
    vols = np.array([vol_of_level(z) for z in probe], dtype=float)
    # The fill is monotone in level by construction; enforce it so a noisy
    # measured curve cannot make np.interp return nonsense.
    vols = np.maximum.accumulate(vols)
    v_full = float(vols[-1])
    if v_full <= 0.0:
        raise ValueError(f"no impounded volume anywhere in [{z_lo}, {z_hi}]")

    steps = np.diff(vols)
    typical = float(np.median(steps[steps > 0])) if np.any(steps > 0) else 0.0
    sills = [{"level_m": round(float(probe[i + 1]), 2),
              "volume_jump_mcm": round(float(steps[i]) / 1e6, 3),
              "vs_typical_step": round(float(steps[i] / typical), 1) if typical else None}
             for i in np.argsort(steps)[::-1][:4]
             if typical and steps[i] > 3.0 * typical]

    targets = np.linspace(v_full / n_frames, v_full, n_frames)
    raw = [float(np.interp(t, vols, probe)) for t in targets]

    # Deduplicate: a level that repeats is a target that fell inside a sill gap.
    # Nudge it up the level axis instead, so every frame is a distinct pool.
    min_gap = (z_hi - z_lo) / (n_probe * 4.0)
    out: list[float] = []
    for lv in raw:
        if out and lv - out[-1] < min_gap:
            lv = out[-1] + min_gap
        out.append(min(lv, z_hi))
    return out, sills


def lake_formation_levels(
    dem_array: np.ndarray,
    transform,
    seed_xy: tuple[float, float],
    wse_m: float,
    n_frames: int,
    barrier_mask: Optional[np.ndarray] = None,
    barrier_crest_m: Optional[float] = None,
    n_probe: int = 240,
) -> tuple[list[float], list[dict], float]:
    """Equal-VOLUME levels for a pre-breach lake-formation animation.

    Returns ``(levels_m, sill_merges, z_min_m)``. Feed ``levels_m`` straight to
    ``compute_lake_depth_grids(levels_m=...)``.

    This exists in `fill.py` rather than in a script because BOTH
    `run_pipeline.py` and `scripts/make_lake_formation.py` need it, and when the
    script owned it the pipeline kept its own stage-fraction version: the
    animation the API served was four frames of which two were empty
    (0.00, 0.00, 0.25, 19.54 MCM) while the script produced 24 usable ones off
    the same DEM. One implementation, one behaviour.
    """
    dem_work, (row, col), _px, volume_at = _pool_machinery(
        dem_array, transform, seed_xy, barrier_mask, barrier_crest_m)

    below = dem_work < wse_m
    if not below[row, col]:
        raise ValueError(f"seed is not below the target level {wse_m}")
    labels, _ = ndimage.label(below)
    full_pool = labels == labels[row, col]
    z_min = float(dem_work[full_pool].min())

    levels, sills = equal_volume_levels(volume_at, z_min, float(wse_m),
                                        n_frames, n_probe=n_probe)
    return levels, sills, z_min


def level_for_volume_fraction(
    dem_array: np.ndarray,
    transform,
    seed_xy: tuple[float, float],
    wse_m: float,
    fraction: float,
    barrier_mask: Optional[np.ndarray] = None,
    barrier_crest_m: Optional[float] = None,
    tol_rel: float = 1e-4,
    max_iter: int = 60,
) -> float:
    """Water level that impounds ``fraction`` of the full pool's VOLUME.

    ``compute_lake_depth_grids(fractions=...)`` reads its fractions as **STAGE**
    fractions: ``level = z_min + f * (wse_m - z_min)``. In a gorge that is not
    the same number as a volume fraction and is not close to it. Measured on
    phutkal at 55.8 m cells, 2026-09-18:

        0.9 STAGE fill  -> level 3798.35 m, 22.044 MCM = 0.8044 of the pool
        0.9 VOLUME fill -> level 3801.76 m, 24.666 MCM = 0.9000 of the pool

    That 10.6 % gap is not cosmetic: ``run_pipeline`` used ``reservoir_fill`` as
    a VOLUME fraction for ``impounded_vol_m3`` (``geom.volume_m3 *
    reservoir_fill``) and as a STAGE fraction for the initial condition, 217
    lines apart, believing the two consistent -- the comment at the call site
    said so explicitly. Validity gate G1 compares exactly those two quantities
    and failed at **ratio 0.8937** against a 0.95-1.05 tolerance, reporting
    "water in the flood did not come from the impoundment" when the water had in
    fact come from the impoundment and the two sides simply meant different
    things by 0.9.

    Bisection, because the hypsometry V(z) genuinely steps: a rising pool that
    tops a sill takes in an adjacent basin in one increment (phutkal gains
    2.488 MCM in one 0.29 m step at 3763.05 m), so no closed form exists and
    interpolating a stage-storage curve across a sill would land inside a gap
    no level occupies.

    Returns ``wse_m`` when the pool has no volume or ``fraction >= 1``.
    """
    if not np.isfinite(fraction):
        raise ValueError("fraction must be finite")
    fraction = float(fraction)

    ends = compute_lake_depth_grids(
        dem_array=dem_array, transform=transform, seed_xy=seed_xy, wse_m=wse_m,
        fractions=(0.0, 1.0), barrier_mask=barrier_mask,
        barrier_crest_m=barrier_crest_m,
    )
    if not ends:
        return float(wse_m)
    z_min = float(ends[0]["level_m"])
    v_full = float(ends[-1]["volume_m3"])
    if v_full <= 0.0 or fraction >= 1.0:
        return float(wse_m)
    if fraction <= 0.0:
        return z_min

    target = fraction * v_full

    def vol_at(level: float) -> float:
        g = compute_lake_depth_grids(
            dem_array=dem_array, transform=transform, seed_xy=seed_xy,
            wse_m=wse_m, levels_m=[level], barrier_mask=barrier_mask,
            barrier_crest_m=barrier_crest_m,
        )
        return float(g[0]["volume_m3"]) if g else 0.0

    lo, hi = z_min, float(wse_m)
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        if vol_at(mid) < target:
            lo = mid
        else:
            hi = mid
        if (hi - lo) <= tol_rel * max(1.0, wse_m - z_min):
            break
    return 0.5 * (lo + hi)
