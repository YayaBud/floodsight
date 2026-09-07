import sys
sys.path.insert(0, r'd:\sih_work\floodsight')

from pathlib import Path
import numpy as np
import rasterio

# Check envelope
envelope_path = Path(r'd:\sih_work\floodsight\data\scenarios\f4aa5f3d\envelope.tif')
if envelope_path.exists():
    with rasterio.open(envelope_path) as src:
        data = src.read(1)
        print("ENVELOPE RASTER:")
        print(f"  Shape: {data.shape}")
        print(f"  Data type: {data.dtype}")
        print(f"  Min value: {np.nanmin(data)}")
        print(f"  Max value: {np.nanmax(data)}")
        print(f"  Mean: {np.nanmean(data)}")
        print(f"  Count > 0: {np.count_nonzero(data > 0)}")
        print(f"  Percentiles: 50%={np.nanpercentile(data, 50)}, 90%={np.nanpercentile(data, 90)}, 99%={np.nanpercentile(data, 99)}")

# Check max_depth
max_depth_path = Path(r'd:\sih_work\floodsight\data\scenarios\f4aa5f3d\max_depth.tif')
if max_depth_path.exists():
    with rasterio.open(max_depth_path) as src:
        data = src.read(1)
        print("\nMAX_DEPTH RASTER:")
        print(f"  Shape: {data.shape}")
        print(f"  Data type: {data.dtype}")
        print(f"  Min value: {np.nanmin(data)}")
        print(f"  Max value: {np.nanmax(data)}")
        print(f"  Mean: {np.nanmean(data)}")
        print(f"  Count > 0: {np.count_nonzero(data > 0)}")
        print(f"  Percentiles: 50%={np.nanpercentile(data, 50)}, 90%={np.nanpercentile(data, 90)}, 99%={np.nanpercentile(data, 99)}")

# Check arrival time
arrival_path = Path(r'd:\sih_work\floodsight\data\scenarios\f4aa5f3d\arrival_time.tif')
if arrival_path.exists():
    with rasterio.open(arrival_path) as src:
        data = src.read(1)
        print("\nARRIVAL_TIME RASTER:")
        print(f"  Shape: {data.shape}")
        print(f"  Data type: {data.dtype}")
        print(f"  Min value: {np.nanmin(data)}")
        print(f"  Max value: {np.nanmax(data)}")
        print(f"  Mean: {np.nanmean(data)}")
        print(f"  Count > 0: {np.count_nonzero(data > 0)}")
        print(f"  Percentiles: 50%={np.nanpercentile(data, 50)}, 90%={np.nanpercentile(data, 90)}, 99%={np.nanpercentile(data, 99)}")
