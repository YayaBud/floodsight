"""
M4 — Land Cover (LULC) to Manning Roughness Mapping
===================================================
Maps land cover classes from ESA WorldCover (10m) and ISRO Bhuvan (LULC)
to physical Manning roughness coefficients n [s / m^(1/3)].

ESA WorldCover class codes:
- 10: Tree cover (forest) -> 0.100
- 20: Shrubland -> 0.065
- 30: Grassland -> 0.040
- 40: Cropland -> 0.045
- 50: Built-up (urban settlements) -> 0.080 (form drag of structures)
- 60: Bare / sparse vegetation (riverbed gravel/scree) -> 0.035
- 70: Snow and ice -> 0.020
- 80: Permanent water bodies (main river channel) -> 0.028
- 90: Herbaceous wetland -> 0.085
- 95: Mangroves -> 0.120
- 100: Moss and lichen -> 0.030
"""
from __future__ import annotations

import logging
from pathlib import Path
import numpy as np
import rasterio

logger = logging.getLogger(__name__)

# Standard ESA WorldCover to Manning lookup table
ESA_WORLDCOVER_MANNING = {
    10: 0.100,  # Tree cover
    20: 0.065,  # Shrubland
    30: 0.040,  # Grassland
    40: 0.045,  # Cropland
    50: 0.080,  # Built-up
    60: 0.035,  # Bare / gravel / scree
    70: 0.020,  # Snow and ice
    80: 0.028,  # Permanent water body
    90: 0.085,  # Wetland
    95: 0.120,  # Mangrove
    100: 0.030, # Moss / lichen
}

DEFAULT_MANNING = 0.045


def map_lulc_to_manning(
    lulc_arr: np.ndarray,
    custom_lut: dict[int, float] | None = None,
    default_n: float = DEFAULT_MANNING,
) -> np.ndarray:
    """
    Convert a 2D array of LULC integer class codes into a 2D array of Manning roughness n.
    """
    lut = {**ESA_WORLDCOVER_MANNING, **(custom_lut or {})}
    out = np.full(lulc_arr.shape, default_n, dtype=np.float64)
    for code, n_val in lut.items():
        out[lulc_arr == code] = float(n_val)
    return out


def load_manning_from_lulc_raster(
    lulc_raster_path: str | Path,
    target_shape: tuple[int, int],
) -> np.ndarray:
    """
    Read a LULC GeoTIFF and return a 2D Manning grid matching target_shape.
    """
    p = Path(lulc_raster_path)
    if not p.exists():
        raise FileNotFoundError(f"LULC raster not found at {p}")

    with rasterio.open(p) as src:
        raw = src.read(1)
        if raw.shape == target_shape:
            lulc_grid = raw
        else:
            from scipy.ndimage import zoom
            zy = target_shape[0] / raw.shape[0]
            zx = target_shape[1] / raw.shape[1]
            lulc_grid = zoom(raw, (zy, zx), order=0)

    return map_lulc_to_manning(lulc_grid)


#: Manning n for a mapped natural channel. Chow (1959), Table 5-6, "natural
#: streams — mountain streams, no vegetation in channel, banks usually steep,
#: bottom gravels/cobbles/few boulders": n = 0.030-0.050, normal 0.040. Kept at
#: the lower-middle of that range because these reaches are the main stem, and
#: overridable per scenario. This is NOT a tuning knob for making water move
#: faster — it is the one roughness class the elevation/population proxy cannot
#: produce, because the proxy has no idea where the river is.
CHANNEL_MANNING_N = 0.035


def apply_channel_roughness(
    manning_grid: np.ndarray,
    river_geoms,
    transform,
    *,
    n_channel: float = CHANNEL_MANNING_N,
    channel_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, dict]:
    """
    Overwrite the mapped river's cells with a channel roughness.

    Every other roughness source in this pipeline is a proxy for land cover:
    height above the thalweg, or GHS-POP population density. Neither knows
    where the channel is, so before this the main stem carried whatever n the
    proxy happened to assign — on a Himalayan tile, the same 0.032 as the rest
    of the valley floor, and in a city the built-up 0.08.

    It matters, measured on the idealised channel (E7): raising the CHANNEL n
    from 0.020 to 0.100 moved the front from 4.26 km to 1.70 km and the peak
    speed from 6.52 to 2.00 m/s, while changing the FLOODPLAIN n over
    0.035-0.150 moved the front not at all (3.12 km in every case), because the
    flow stays in the channel. An undifferentiated grid is therefore pulling on
    the wrong lever.

    Prefer ``channel_mask`` — the cells ``condition_flowline`` actually treated
    as the channel after cross-stream snapping — so the channel's geometry and
    its Manning n refer to the same cells. Rasterising the raw OSM centreline
    instead can land the roughness one or two cells off the conditioned
    thalweg, which is the misregistration that motivated the snap in the first
    place. Falls back to rasterising the centreline with ``all_touched`` when
    no mask is supplied.
    """
    import rasterio.features

    grid = np.asarray(manning_grid, dtype=np.float64).copy()
    if channel_mask is not None and np.asarray(channel_mask).any():
        mask = np.asarray(channel_mask, dtype=bool)
        if mask.shape != grid.shape:
            raise ValueError("channel_mask must match the roughness grid shape")
        source = "conditioned_flowline"
    else:
        geoms = [g for g in river_geoms if g is not None and not g.is_empty]
        if not geoms:
            return grid, {"available": False, "reason": "no mapped river geometry"}
        mask = rasterio.features.rasterize(
            [(g, 1) for g in geoms], out_shape=grid.shape, transform=transform,
            fill=0, dtype="uint8", all_touched=True,
        ).astype(bool)
        source = "osm_centreline"
    if not mask.any():
        return grid, {"available": False, "reason": "river does not resolve at this cell size"}
    before = float(grid[mask].mean())
    grid[mask] = float(n_channel)
    report = {
        "available": True,
        "source": source,
        "n_channel": float(n_channel),
        "channel_cells": int(mask.sum()),
        "channel_fraction": round(float(mask.mean()), 5),
        "mean_n_replaced": round(before, 4),
    }
    logger.info("M4: channel roughness — %d cells (%.2f%%) set to n=%.3f "
                "(they averaged n=%.3f from the land-cover proxy)",
                report["channel_cells"], 100.0 * report["channel_fraction"],
                n_channel, before)
    return grid, report
