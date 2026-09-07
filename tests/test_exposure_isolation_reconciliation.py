import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import numpy as np
import geopandas as gpd
from shapely.geometry import Polygon, Point
import rasterio
from rasterio.transform import from_origin
import networkx as nx
from pathlib import Path
import tempfile
import os

from src.m5_exposure.exposure import compute_village_exposure
from src.m6_isolation.isolation import compute_isolation_times

def test_exposure_isolation_reconciliation():
    """
    Test that M5 (exposure) and M6 (isolation) agree on whether a village
    is inundated (water arrival time is not NaN).
    """
    # Create a 30x30 depth raster (0m depth everywhere, except top left 10x10)
    depth_arr = np.zeros((30, 30), dtype=np.float32)
    depth_arr[:10, :10] = 1.0
    # Transform: origin at (0, 30), 1m per pixel
    transform = from_origin(0, 30, 1, 1)
    crs = "EPSG:32643"
    
    # Village polygon that partially overlaps the flooded area
    poly = Polygon([(5, 25), (5, 27), (7, 27), (7, 25)])
    village_gdf = gpd.GeoDataFrame({
        "village_name": ["Test Village"],
        "pop_total": [100],
    }, geometry=[poly], crs=crs)
    
    # Create a simple road graph that the village can snap to
    G = nx.MultiDiGraph(crs=crs)
    # Add a node at the village centroid
    G.add_node(1, x=6, y=6)
    G.add_node(2, x=0, y=0)
    G.add_edge(1, 2)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        raster_path = Path(tmpdir) / "depth.tif"
        with rasterio.open(
            raster_path, "w", driver="GTiff",
            height=depth_arr.shape[0], width=depth_arr.shape[1], count=1,
            dtype="float32", crs=crs, transform=transform
        ) as dst:
            dst.write(depth_arr, 1)
            
        # Run M5 exposure
        exposure_df = compute_village_exposure(
            inundation_raster=raster_path,
            village_polygons=village_gdf,
            depth_threshold_m=0.3
        )
        
        # Run M6 isolation
        raster_stack = [(300.0, raster_path)]
        isolation_df = compute_isolation_times(
            G=G,
            raster_stack=raster_stack,
            village_gdf=exposure_df,
            max_depth_raster=raster_path,
            threshold_m=0.3
        )
        
        row = isolation_df.iloc[0]
        
        # The core assertion: if M5 says inundated, M6 must give an arrival time
        assert bool(row["inundated"]) is True
        assert not np.isnan(row["water_arrival_time_s"])
        
        # Test case 2: Not inundated
        poly_safe = Polygon([(20, 20), (20, 22), (22, 22), (22, 20)])
        village_safe_gdf = gpd.GeoDataFrame({
            "village_name": ["Safe Village"],
            "pop_total": [100],
        }, geometry=[poly_safe], crs=crs)
        
        exposure_safe_df = compute_village_exposure(
            inundation_raster=raster_path,
            village_polygons=village_safe_gdf,
            depth_threshold_m=0.3
        )
        
        G_safe = nx.MultiDiGraph(crs=crs)
        G_safe.add_node(1, x=21, y=21)
        G_safe.add_node(2, x=22, y=22)
        G_safe.add_edge(1, 2)
        
        isolation_safe_df = compute_isolation_times(
            G=G_safe,
            raster_stack=raster_stack,
            village_gdf=exposure_safe_df,
            max_depth_raster=raster_path,
            threshold_m=0.3
        )
        
        row_safe = isolation_safe_df.iloc[0]
        assert bool(row_safe["inundated"]) is False
        assert np.isnan(row_safe["water_arrival_time_s"])
