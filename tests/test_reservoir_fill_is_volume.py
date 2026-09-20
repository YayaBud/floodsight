"""`reservoir_fill` is a VOLUME fraction, and the initial condition must honour it.

The defect this pins, measured on phutkal at 55.8 m cells on 2026-09-18:

    reservoir_fill = 0.9
      run_pipeline.py:895   impounded_vol_m3 = geom.volume_m3 * 0.9   -> VOLUME
      run_pipeline.py:1112  compute_lake_depth_grids(fractions=(0.9,)) -> STAGE

`compute_lake_depth_grids` derives `level = z_min + f*(wse - z_min)`, so a 0.9
STAGE fill held 22.044 MCM where a 0.9 VOLUME fill holds 24.666 MCM -- 0.8044 of
the pool against 0.9000. The two call sites sit 217 lines apart and the comment
at the second one asserted they were consistent.

Validity gate G1 compares exactly those two quantities. It failed at **ratio
0.8937** against a 0.95-1.05 tolerance and reported "water in the flood did not
come from the impoundment" -- an accusation that was false. The water came from
the impoundment; the two sides disagreed about what 0.9 meant.

These tests fail if `level_for_volume_fraction` stops honouring volume, or if a
future edit routes the initial condition back through a stage fraction.
"""

from __future__ import annotations

import numpy as np
import pytest
from rasterio.transform import from_origin

from src.m2_geometry.fill import compute_lake_depth_grids, level_for_volume_fraction


def _gorge(nx=60, ny=60, cell=50.0):
    """A V-shaped gorge that widens with height, so stage != volume.

    Volume grows faster than stage here for the same reason it does on a real
    gorge: the cross-section widens upward, so the top of the range holds
    disproportionately more water than the bottom.
    """
    yy, xx = np.mgrid[0:ny, 0:nx]
    # floor falls to the south; walls rise steeply away from the centreline
    z = 100.0 - yy * 0.30 + np.abs(xx - nx // 2) * 2.0
    # a wall across the downstream end so the pool is closed
    z[ny - 1, :] = 500.0
    z[:, 0] = 500.0
    z[:, nx - 1] = 500.0
    z[0, :] = 500.0
    transform = from_origin(0.0, ny * cell, cell, cell)
    seed_xy = (nx // 2 * cell + cell / 2, (ny * cell) - (ny // 2 * cell) - cell / 2)
    return z.astype(float), transform, seed_xy


@pytest.mark.parametrize("fraction", [0.25, 0.5, 0.75, 0.9])
def test_volume_fraction_level_holds_that_fraction_of_volume(fraction):
    z, transform, seed_xy = _gorge()
    wse = float(np.percentile(z[z < 500.0], 92))

    full = compute_lake_depth_grids(dem_array=z, transform=transform,
                                    seed_xy=seed_xy, wse_m=wse, fractions=(1.0,))
    assert full and full[0]["volume_m3"] > 0.0, "fixture impounds nothing"
    v_full = full[0]["volume_m3"]

    level = level_for_volume_fraction(dem_array=z, transform=transform,
                                      seed_xy=seed_xy, wse_m=wse, fraction=fraction)
    got = compute_lake_depth_grids(dem_array=z, transform=transform, seed_xy=seed_xy,
                                   wse_m=wse, levels_m=[level])[0]["volume_m3"]

    assert got / v_full == pytest.approx(fraction, abs=0.01), (
        f"level {level:.3f} holds {got/v_full:.4f} of the pool, asked for {fraction}")


def test_stage_and_volume_fractions_actually_differ():
    """If these ever coincide the fixture has stopped exercising the defect."""
    z, transform, seed_xy = _gorge()
    wse = float(np.percentile(z[z < 500.0], 92))

    grids = compute_lake_depth_grids(dem_array=z, transform=transform,
                                     seed_xy=seed_xy, wse_m=wse,
                                     fractions=(0.0, 0.9, 1.0))
    z_min, v_stage, v_full = (grids[0]["level_m"], grids[1]["volume_m3"],
                              grids[2]["volume_m3"])
    stage_share = v_stage / v_full

    level_vol = level_for_volume_fraction(dem_array=z, transform=transform,
                                          seed_xy=seed_xy, wse_m=wse, fraction=0.9)
    level_stage = z_min + 0.9 * (wse - z_min)

    assert stage_share < 0.88, (
        f"a 0.9 STAGE fill holds {stage_share:.4f} of the volume on this fixture; "
        f"it is no longer a gorge and the test proves nothing")
    assert level_vol > level_stage, (
        "the volume-fraction level must sit ABOVE the stage-fraction level in a "
        "section that widens upward")


def test_degenerate_fractions_are_clamped():
    z, transform, seed_xy = _gorge()
    wse = float(np.percentile(z[z < 500.0], 92))
    assert level_for_volume_fraction(dem_array=z, transform=transform,
                                     seed_xy=seed_xy, wse_m=wse,
                                     fraction=1.0) == pytest.approx(wse)
    assert level_for_volume_fraction(dem_array=z, transform=transform,
                                     seed_xy=seed_xy, wse_m=wse,
                                     fraction=1.5) == pytest.approx(wse)
    low = level_for_volume_fraction(dem_array=z, transform=transform,
                                    seed_xy=seed_xy, wse_m=wse, fraction=0.0)
    assert low < wse
    with pytest.raises(ValueError):
        level_for_volume_fraction(dem_array=z, transform=transform, seed_xy=seed_xy,
                                  wse_m=wse, fraction=float("nan"))
