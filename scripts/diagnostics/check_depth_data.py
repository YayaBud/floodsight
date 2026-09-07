import json
import sys
sys.path.insert(0, r'd:\sih_work\floodsight')

from pathlib import Path
import numpy as np
import rasterio

# Check if depth rasters exist
depth_dir = Path(r'd:\sih_work\floodsight\data\scenarios\f4aa5f3d\depth_rasters')
if depth_dir.exists():
    files = sorted(depth_dir.glob("depth_*.tif"))
    print(f"Found {len(files)} depth rasters")
    
    if files:
        # Check first raster
        with rasterio.open(files[0]) as src:
            data = src.read(1)
            print(f"\nFirst raster shape: {data.shape}")
            print(f"Data type: {data.dtype}")
            print(f"Min value: {np.nanmin(data)}")
            print(f"Max value: {np.nanmax(data)}")
            print(f"Mean: {np.nanmean(data)}")
            print(f"Std: {np.nanstd(data)}")
            print(f"Count > 0: {np.count_nonzero(data > 0)}")
            print(f"Count > 0.01: {np.count_nonzero(data > 0.01)}")
            print(f"Unique values (sample): {np.unique(data)[:20]}")
else:
    print(f"No depth_rasters directory at {depth_dir}")

# Also check the solver output itself
import os
job_output = Path(r'd:\sih_work\floodsight\data\scenarios\f4aa5f3d')
print(f"\nJob output directory contents:")
for item in sorted(job_output.glob("*")):
    if item.is_dir():
        file_count = len(list(item.glob("*")))
        print(f"  {item.name}/ ({file_count} files)")
    else:
        print(f"  {item.name}")
