"""Lake-formation frames are spaced by VOLUME, and one implementation does it.

Two defects this pins, both measured 2026-09-18/19 on phutkal.

**Stage vs volume.** `compute_lake_depth_grids(fractions=...)` reads its
fractions as STAGE fractions, `z_min + f*(wse - z_min)`. On a gorge that is not
an even animation: 3745-3765 m is 29 % of the height range and holds 12.9 % of
the water, 3785-3805 m is the same 29 % and holds 47 %. The pipeline's four
frames at 0.25/0.50/0.75/0.95 stage held **0.49, 2.16, 3.86 and 11.77 MCM** of a
~27 MCM pool, so two rendered as nothing and the frame labelled "25% Capacity"
was **1.8 %** of the pool.

**Two implementations.** `scripts/make_lake_formation.py` was fixed on
2026-09-18 and produced 24 usable frames, while `run_pipeline.py` kept the
stage-fraction version — and the pipeline is what `/api/lake_formation/{job}`
serves. The solver now lives in `src/m2_geometry/fill.py` and both call it.
"""

from __future__ import annotations

import numpy as np
import pytest
from rasterio.transform import from_origin

from src.m2_geometry.fill import (_pool_machinery, compute_lake_depth_grids,
                                  equal_volume_levels, lake_formation_levels)


def _gorge(nx=70, ny=70, cell=40.0):
    """A section that widens upward, so stage and volume genuinely differ."""
    yy, xx = np.mgrid[0:ny, 0:nx]
    z = 100.0 - yy * 0.25 + np.abs(xx - nx // 2) * 2.2
    z[ny - 1, :] = 500.0
    z[0, :] = 500.0
    z[:, 0] = 500.0
    z[:, nx - 1] = 500.0
    transform = from_origin(0.0, ny * cell, cell, cell)
    seed_xy = (nx // 2 * cell + cell / 2, (ny * cell) - (ny // 2 * cell) - cell / 2)
    return z.astype(float), transform, seed_xy


def test_probe_agrees_with_compute_lake_depth_grids():
    """`_pool_machinery` duplicates three lines of barrier emplacement.

    This is the guard the docstring promises: if the copy ever drifts from
    `compute_lake_depth_grids`, the two volumes stop matching and this fails.
    """
    z, transform, seed_xy = _gorge()
    wse = float(np.percentile(z[z < 500.0], 90))
    barrier = np.zeros_like(z, dtype=bool)
    barrier[55, 25:45] = True
    crest = wse + 5.0

    _work, _rc, _px, volume_at = _pool_machinery(z, transform, seed_xy, barrier, crest)

    for lv in np.linspace(float(z.min()) + 1.0, wse, 7):
        viaprobe = volume_at(float(lv))
        viagrid = compute_lake_depth_grids(
            dem_array=z, transform=transform, seed_xy=seed_xy, wse_m=wse,
            levels_m=[float(lv)], barrier_mask=barrier, barrier_crest_m=crest,
        )[0]["volume_m3"]
        # 1e-6, not tighter: `compute_lake_depth_grids` casts its depth grid to
        # float32 before summing, so the two accumulate differently and differ
        # by ~1.2e-7 relative on this fixture. That is dtype, not drift. Real
        # drift -- a barrier emplaced differently, or the wrong connected
        # component -- moves the volume by orders of magnitude, so the guard
        # still bites. An earlier 1e-9 here failed for no good reason.
        assert viaprobe == pytest.approx(viagrid, rel=1e-6, abs=1e-6), (
            f"the probe and compute_lake_depth_grids disagree at {lv:.2f} m: "
            f"{viaprobe:.4e} vs {viagrid:.4e} - _pool_machinery has drifted")


def test_frames_are_evenly_spaced_in_volume_not_height():
    z, transform, seed_xy = _gorge()
    wse = float(np.percentile(z[z < 500.0], 90))
    n = 12

    levels, _sills, z_min = lake_formation_levels(
        dem_array=z, transform=transform, seed_xy=seed_xy, wse_m=wse, n_frames=n)
    assert len(levels) == n
    assert z_min <= levels[0] <= levels[-1] <= wse

    grids = compute_lake_depth_grids(dem_array=z, transform=transform,
                                     seed_xy=seed_xy, wse_m=wse, levels_m=levels)
    vols = np.array([g["volume_m3"] for g in grids])
    steps = np.diff(vols)
    assert (steps > 0).all(), "volume must increase every frame"

    # Equal-volume: the spread of increments is small relative to their median.
    med = float(np.median(steps))
    assert float(steps.std()) / med < 0.35, (
        f"increments are not even: median {med:.3e}, std {steps.std():.3e}")

    # And it must beat the stage spacing it replaced, on the same terrain.
    stage = compute_lake_depth_grids(
        dem_array=z, transform=transform, seed_xy=seed_xy, wse_m=wse,
        fractions=tuple((i + 1) / n for i in range(n)))
    stage_steps = np.diff([g["volume_m3"] for g in stage])
    assert float(steps.std()) / med < float(stage_steps.std()) / max(
        float(np.median(stage_steps)), 1e-9), (
        "equal-volume spacing is no more even than the stage spacing it replaced")


def test_no_frame_is_empty():
    """Two of the pipeline's four old frames held 0.00 MCM. None may now."""
    z, transform, seed_xy = _gorge()
    wse = float(np.percentile(z[z < 500.0], 90))
    levels, _s, _zm = lake_formation_levels(
        dem_array=z, transform=transform, seed_xy=seed_xy, wse_m=wse, n_frames=24)
    grids = compute_lake_depth_grids(dem_array=z, transform=transform,
                                     seed_xy=seed_xy, wse_m=wse, levels_m=levels)
    empty = [i for i, g in enumerate(grids) if g["volume_m3"] <= 0.0]
    assert not empty, f"frames {empty} impound nothing"


def test_sills_are_reported_not_smoothed():
    """A pool topping a sill is a real feature; it must be reported."""
    z, transform, seed_xy = _gorge()
    # carve a side basin that the rising pool will capture in one step
    z[20:28, 5:15] = 70.0
    wse = float(np.percentile(z[z < 500.0], 90))
    levels, sills, _zm = lake_formation_levels(
        dem_array=z, transform=transform, seed_xy=seed_xy, wse_m=wse, n_frames=24)
    assert len(levels) == len(set(round(v, 6) for v in levels)), (
        "levels collapsed onto each other instead of being nudged apart")
    for s in sills:
        assert {"level_m", "volume_jump_mcm", "vs_typical_step"} <= set(s)


def test_degenerate_input_raises_rather_than_returning_nothing():
    z, transform, seed_xy = _gorge()
    # a level below the pool floor impounds nothing anywhere
    with pytest.raises(ValueError):
        lake_formation_levels(dem_array=z, transform=transform, seed_xy=seed_xy,
                              wse_m=float(z.min()) - 10.0, n_frames=6)


def test_equal_volume_levels_is_pure_and_shared():
    """The solver takes a callable, so both backends can drive it."""
    calls = []

    def fake_vol(level: float) -> float:
        calls.append(level)
        return max(0.0, (level - 10.0)) ** 2      # smooth, strictly increasing

    levels, sills = equal_volume_levels(fake_vol, 10.0, 20.0, 5, n_probe=50)
    assert len(levels) == 5
    assert levels == sorted(levels)
    assert sills == [], "a smooth curve has no sills"
    assert len(calls) == 50, "the probe count must be honoured"
