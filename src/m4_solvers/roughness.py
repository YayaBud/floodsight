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
