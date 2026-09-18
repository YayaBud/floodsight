import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from pathlib import Path
import tempfile
import numpy as np
import rasterio
from rasterio.transform import from_origin
import geopandas as gpd
from shapely.geometry import Polygon

from src.m2_geometry.dem_utils import prepare_custom_dem
from src.m3_breach import DamGeometry, FailureMechanism
from src.m3_breach.ensemble import build_ensemble
from src.m4_solvers.roughness import map_lulc_to_manning, ESA_WORLDCOVER_MANNING
from src.m5_exposure.exposure import load_custom_population_csv, compute_village_exposure
from src.provenance import Provenance


def test_prepare_custom_dem():
    """Verify that an arbitrary custom DEM GeoTIFF is accepted, reprojected, and tagged COMPUTED_LIVE."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_dem = Path(tmpdir) / "drone_survey.tif"
        data = np.full((50, 50), 3800.0, dtype=np.float32)
        transform = from_origin(77.0, 33.0, 0.0001, 0.0001)
        
        with rasterio.open(
            tmp_dem, "w", driver="GTiff", height=50, width=50, count=1,
            dtype="float32", crs="EPSG:4326", transform=transform
        ) as dst:
            dst.write(data, 1)
            
        out_dir = Path(tmpdir) / "out"
        reproj_path, prov = prepare_custom_dem(tmp_dem, out_dir)
        
        assert reproj_path.exists()
        assert prov == Provenance.COMPUTED_LIVE
        with rasterio.open(reproj_path) as res:
            assert res.crs.is_projected  # Converted to metric UTM


def test_cwc_dam_engineering_override():
    """Verify that providing engineering specs (crest length, dam type) clamps breach geometry."""
    # Without engineering specs (pure empirical regression)
    dam_empirical = DamGeometry(
        height_m=70.0,
        volume_m3=25e6,
        dam_height_m=80.0,
        failure_mechanism=FailureMechanism.OVERTOPPING_EROSION,
    )
    ens_emp = build_ensemble(dam_empirical)
    unconstrained_width = ens_emp.central.breach_width_m
    unconstrained_qp = ens_emp.central.peak_discharge_m3s

    # With engineering dossier: crest length is only 40m, dam is concrete
    dam_engineered = DamGeometry(
        height_m=70.0,
        volume_m3=25e6,
        dam_height_m=80.0,
        failure_mechanism=FailureMechanism.OVERTOPPING_EROSION,
        crest_length_m=40.0,
        dam_type="concrete",
    )
    ens_eng = build_ensemble(dam_engineered)
    
    # Breach width must be clamped by crest length
    assert ens_eng.central.breach_width_m <= 40.0
    # Concrete failure slows down erosion, reducing peak outflow compared to unconstrained earthfill
    assert ens_eng.central.formation_time_h > ens_emp.central.formation_time_h


def test_lulc_to_manning_mapping():
    """Verify that ESA WorldCover land cover codes convert to exact physical roughness values."""
    # Create synthetic LULC grid
    lulc = np.array([
        [10, 50],  # 10=Tree canopy (forest), 50=Built-up (urban)
        [60, 80],  # 60=Bare rock/riverbed gravel, 80=Permanent water
    ], dtype=np.int32)
    
    manning = map_lulc_to_manning(lulc)
    assert manning.shape == (2, 2)
    assert np.isclose(manning[0, 0], ESA_WORLDCOVER_MANNING[10])  # Forest n=0.100
    assert np.isclose(manning[0, 1], ESA_WORLDCOVER_MANNING[50])  # Urban n=0.080
    assert np.isclose(manning[1, 0], ESA_WORLDCOVER_MANNING[60])  # Riverbed scree n=0.035
    assert np.isclose(manning[1, 1], ESA_WORLDCOVER_MANNING[80])  # Water n=0.028


def test_custom_population_csv_override():
    """Verify that surveyed local headcounts (e.g. labor camp / pilgrim count) override generic census."""
    with tempfile.TemporaryDirectory() as tmpdir:
        csv_path = Path(tmpdir) / "field_survey.csv"
        csv_path.write_text(
            "village_name,pop_total,migrant_workers\n"
            "Tapovan Tunnel Camp,204,180\n"
            "Safe Hamlet,45,0\n",
            encoding="utf-8"
        )
        
        pop_map = load_custom_population_csv(csv_path)
        assert pop_map["tapovan tunnel camp"] == 204.0
        assert pop_map["safe hamlet"] == 45.0
        
        # Test in compute_village_exposure
        depth_arr = np.full((20, 20), 2.0, dtype=np.float32)
        transform = from_origin(0, 20, 1, 1)
        raster_path = Path(tmpdir) / "depth.tif"
        with rasterio.open(
            raster_path, "w", driver="GTiff", height=20, width=20, count=1,
            dtype="float32", crs="EPSG:32643", transform=transform
        ) as dst:
            dst.write(depth_arr, 1)
            
        poly = Polygon([(2, 2), (2, 8), (8, 8), (8, 2)])
        gdf = gpd.GeoDataFrame({
            "village_name": ["Tapovan Tunnel Camp"],
            "pop_total": [0],  # GHS-POP default zero
        }, geometry=[poly], crs="EPSG:32643")
        
        exp = compute_village_exposure(
            inundation_raster=raster_path,
            village_polygons=gdf,
            custom_population=pop_map,
        )
        
        row = exp.iloc[0]
        assert row["pop_total"] == 204.0
        assert row["pop_at_risk"] == 204.0
        assert row["pop_provenance"] == Provenance.COMPUTED_LIVE
