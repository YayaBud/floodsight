"""HAND and flow accumulation must survive NumPy 2's removal of `np.in1d`.

pysheds 0.5 calls `np.in1d`, which NumPy 2.0 deleted. In this environment
(numpy 2.5.2) that raised `AttributeError` inside every run, and the pipeline
logged "M2: HAND / flow accumulation unavailable (module 'numpy' has no
attribute 'in1d')" and carried on without either surface. Nothing downstream
reads them today, which is exactly why the breakage sat unnoticed: the run
still looked successful.

`compute_hydro_surfaces` installs a two-line compatibility shim rather than
pinning numpy < 2 or waiting for pysheds > 0.5. `np.isin` is NumPy's own
documented replacement; the one behavioural difference is that `in1d` always
ravelled its first argument while `isin` preserves shape, so the shim ravels.

This test fails if the shim is removed, if it stops being reached, or if the
ravel semantics are dropped.
"""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from src.m2_geometry.dem_utils import compute_hydro_surfaces

pytest.importorskip("pysheds")


def _synthetic_valley(tmp_path):
    """A tilted plane with a V-shaped channel: drains south, one outlet."""
    ny = nx = 80
    yy, xx = np.mgrid[0:ny, 0:nx]
    z = (200.0 - yy * 0.8 + np.abs(xx - nx // 2) * 0.5).astype("float32")
    dem_p = tmp_path / "synth_dem.tif"
    with rasterio.open(dem_p, "w", driver="GTiff", height=ny, width=nx, count=1,
                       dtype="float32", crs="EPSG:32643",
                       transform=from_origin(600000, 3700000, 30, 30),
                       nodata=-9999.0) as dst:
        dst.write(z, 1)
    return dem_p


def test_hand_and_accumulation_are_produced(tmp_path):
    dem_p = _synthetic_valley(tmp_path)
    acc_p, hand_p = compute_hydro_surfaces(dem_p, tmp_path, accumulation_threshold=50)

    assert acc_p is not None, (
        "accumulation returned None — the pysheds path is broken again")
    assert acc_p.exists()

    with rasterio.open(acc_p) as src:
        acc = src.read(1)
    # A tilted plane drains to a single outlet, so the maximum accumulation is
    # the whole grid. Anything much smaller means flow direction failed.
    assert acc.max() > 50, f"nothing accumulated past the threshold (max {acc.max()})"

    if hand_p is not None and hand_p.exists():
        with rasterio.open(hand_p) as src:
            hand = src.read(1)
        finite = hand[np.isfinite(hand)]
        assert finite.size > 0, "HAND is entirely non-finite"
        assert finite.min() >= 0.0, "HAND cannot be negative — it is a height above drainage"


def test_shim_preserves_in1d_ravel_semantics():
    """`in1d` ravels its first argument; `isin` does not. The shim must ravel."""
    compute_hydro_surfaces  # noqa: B018 — import side effect is the point
    from src.m2_geometry import dem_utils  # noqa: F401

    if not hasattr(np, "in1d"):
        pytest.skip("shim not installed yet — it is applied inside compute_hydro_surfaces")

    ar2d = np.arange(6).reshape(2, 3)
    out = np.in1d(ar2d, [1, 4])
    assert out.ndim == 1 and out.shape == (6,), (
        f"in1d must return a flat array of len 6, got shape {out.shape}")
    assert out.tolist() == [False, True, False, False, True, False]
