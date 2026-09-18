"""The conditioner's floor must not be set by the artefact it exists to catch.

Measured defect, 2026-09-13 (24 h Annamayya run): 279 cells of the conditioned
bed sat at exactly 0.00 m in rows 32-362, cols 226-228. `condition_dem` computed
its floor as `percentile(valid, 0.1) - margin_m`, and because those 279 zeros
were themselves in `valid`, `percentile(valid, 0.1)` WAS 0.00 m -- floor -100 m,
nothing walled. 11.38 MCM drained into them and 14.19 MCM left across the
boundary beside them: 22 % of the flood disposed of by a DEM fringe.

Nothing pinned this. These tests do.
"""
import numpy as np
import pytest

from src.m2_geometry.dem_utils import condition_dem

REAL = 200.0          # plausible valley floor for the Annamayya domain
PEAK = 600.0


def _dem_with_zero_fringe(n_zero: int = 279, shape=(400, 300)) -> np.ndarray:
    """Real terrain 200-600 m with a column fringe blended to exactly 0.00 m.

    `n_zero` is large enough that the zeros own the 0.1 percentile, which is
    the precondition of the defect: 279 / 120000 = 0.23 % > 0.1 %.
    """
    z = np.linspace(REAL, PEAK, shape[0] * shape[1]).reshape(shape)
    flat = z.ravel()
    flat[:n_zero] = 0.0
    return flat.reshape(shape)


def test_zero_cells_are_walled_even_when_they_set_the_percentile():
    raw = _dem_with_zero_fringe()
    # Precondition: the artefact owns the statistic. Without this the test
    # would pass for the wrong reason.
    assert float(np.percentile(raw, 0.1)) == 0.0

    cond, mask = condition_dem(raw, nodata=-9999.0)

    zero_cells = raw == 0.0
    assert mask[zero_cells].all(), "zero-elevation cells left unwalled"
    assert (cond[zero_cells] > raw[~zero_cells].max()).all(), \
        "walled cells must sit above the domain maximum, not at it"
    assert not (cond < REAL / 2).any(), "conditioned bed still holds a hole"


def test_sourced_floor_overrides_the_statistic():
    """floor_m is absolute: everything below it is walled, percentile ignored."""
    raw = _dem_with_zero_fringe()
    raw = np.where(raw == 0.0, 0.0, raw)
    raw[10, :5] = 55.0                    # near-zero blend, above 0 but not real

    cond, mask = condition_dem(raw, nodata=-9999.0, floor_m=180.0 - 100.0)

    assert mask[10, :5].all(), "cells below the sourced floor survived"
    assert not (cond[~mask] < 80.0).any()


def test_real_terrain_is_untouched():
    """No zeros, no no-data, nothing implausible -> nothing is walled."""
    raw = np.linspace(REAL, PEAK, 200 * 200).reshape(200, 200)
    cond, mask = condition_dem(raw, nodata=-9999.0)
    assert not mask.any()
    assert np.array_equal(cond, raw)


def test_nodata_still_walled():
    raw = np.full((50, 50), 300.0)
    raw[0, :] = -9999.0
    cond, mask = condition_dem(raw, nodata=-9999.0)
    assert mask[0, :].all()
    assert (cond[0, :] > 300.0).all()


def test_all_bad_raises():
    with pytest.raises(ValueError, match="no valid cells"):
        condition_dem(np.zeros((10, 10)), nodata=-9999.0)
