"""
FloodSight — Shared raster helpers
==================================
Small utilities used by more than one analysis module.

``sample_raster`` exists because the original M5 and M6 code sampled rasters by
calling ``src.read(1)`` inside a per-feature loop — re-reading the entire raster
once per building and once per road edge, per timestep. Reading the band once
and indexing it is the whole optimisation.
"""

from __future__ import annotations

import numpy as np
import rasterio


def sample_raster(arr: np.ndarray, transform, xs, ys) -> np.ndarray:
    """
    Sample a 2-D raster array at map coordinates, vectorised.

    Parameters
    ----------
    arr       : 2-D band array, already read.
    transform : the raster's affine transform.
    xs, ys    : map coordinates in the raster's CRS.

    Returns
    -------
    Values at those points. Points falling outside the raster return 0.0 rather
    than raising: off-domain features are dry by definition, because the raster
    bounds the simulated area. NaNs are converted to 0.0 for the same reason.
    """
    xs = np.atleast_1d(np.asarray(xs, dtype=float))
    ys = np.atleast_1d(np.asarray(ys, dtype=float))
    if xs.size == 0:
        return np.zeros(0)

    rows, cols = rasterio.transform.rowcol(transform, xs, ys)
    rows = np.atleast_1d(np.asarray(rows))
    cols = np.atleast_1d(np.asarray(cols))

    ny, nx = arr.shape
    inside = (rows >= 0) & (rows < ny) & (cols >= 0) & (cols < nx)

    out = np.zeros(xs.shape, dtype=float)
    out[inside] = arr[rows[inside], cols[inside]]
    return np.nan_to_num(out, nan=0.0)


def demo() -> None:
    """Self-check: out-of-bounds must be dry, not an exception."""
    from rasterio.transform import from_origin

    arr = np.arange(12, dtype=float).reshape(3, 4)   # 3 rows, 4 cols
    tr = from_origin(0.0, 3.0, 1.0, 1.0)             # 1 m cells, origin top-left

    # Centre of the top-left cell -> arr[0, 0]
    assert sample_raster(arr, tr, [0.5], [2.5])[0] == 0.0
    # Centre of cell (row 1, col 2) -> arr[1, 2] == 6
    assert sample_raster(arr, tr, [2.5], [1.5])[0] == 6.0
    # Well outside the raster -> 0.0, no exception
    assert sample_raster(arr, tr, [999.0], [999.0])[0] == 0.0
    # Empty input is allowed
    assert sample_raster(arr, tr, [], []).size == 0
    # NaN cells read as 0.0
    arr2 = arr.copy(); arr2[1, 2] = np.nan
    assert sample_raster(arr2, tr, [2.5], [1.5])[0] == 0.0
    print("rasterutils: all checks passed")


if __name__ == "__main__":
    demo()
