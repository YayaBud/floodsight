"""
Tests: CRS consistency at every module boundary.
One rule: everything in the local UTM zone before spatial computation.
Run: pytest tests/test_crs.py -v
"""

import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_utm_zone_detection():
    """UTM zone auto-detection for an Indian coordinate should return EPSG:32643/44 (zone 43/44N)."""
    rasterio = pytest.importorskip("rasterio")
    from rasterio.crs import CRS
    from rasterio.transform import from_bounds
    import numpy as np

    # Create a tiny synthetic DEM at WGS84 centred on J&K (~76.9E, 33.2N → zone 43N = EPSG:32643)
    from src.m2_geometry.dem_utils import _utm_crs_from_bounds
    from rasterio.transform import from_bounds as fb

    class FakeBounds:
        left   = 76.5
        right  = 77.5
        bottom = 33.0
        top    = 34.0

    wgs84 = CRS.from_epsg(4326)
    utm   = _utm_crs_from_bounds(FakeBounds(), wgs84)
    assert utm.to_epsg() in (32643, 32644), f"Expected UTM zone 43/44N, got EPSG:{utm.to_epsg()}"


def test_breach_params_positive():
    """All breach parameter outputs must be positive (no NaN, no negative)."""
    from src.m3_breach import DamGeometry
    from src.m3_breach import froehlich, von_thun, macdonald

    dam = DamGeometry(height_m=25.0, volume_m3=80e6, dam_height_m=28.0, failure_mode="overtopping")
    for module in [froehlich, von_thun, macdonald]:
        p = module.compute(dam)
        assert p.breach_width_m    > 0, f"{p.method}: breach_width_m <= 0"
        assert p.formation_time_h  > 0, f"{p.method}: formation_time_h <= 0"
        assert p.peak_discharge_m3s > 0, f"{p.method}: peak_discharge_m3s <= 0"
        assert p.side_slope_hv     > 0, f"{p.method}: side_slope_hv <= 0"
