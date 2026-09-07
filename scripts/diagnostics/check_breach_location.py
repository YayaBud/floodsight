import sys
sys.path.insert(0, r'd:\sih_work\floodsight')

from pathlib import Path
import json
import rasterio
import numpy as np
from rasterio.transform import Affine
from pyproj import Transformer

# Load DEM
dem_path = Path(r'd:\sih_work\floodsight\data\dem\phutkal_dem.tif')
with rasterio.open(dem_path) as src:
    dem = src.read(1)
    transform = src.transform
    crs = src.crs
    print(f"DEM shape: {dem.shape}, CRS: {crs}")
    print(f"DEM min: {dem.min()}, max: {dem.max()}")

# Phutkal scenario config
breach_lon, breach_lat = 77.050, 33.250
wse_m = 3850.0
thalweg_m = 3790.0

# Convert WGS84 to DEM coordinates
to_dem = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
bx, by = to_dem.transform(breach_lon, breach_lat)
print(f"\nBreach location WGS84: ({breach_lon}, {breach_lat})")
print(f"Breach location projected: ({bx:.1f}, {by:.1f})")

# Get grid index
from rasterio.transform import Affine
rowcol = rasterio.transform.rowcol
iy, ix = rowcol(transform, bx, by)
print(f"Breach grid index: row={iy}, col={ix}")

# Check bounds
ny, nx = dem.shape
print(f"DEM size: {nx} x {ny}")
if 0 <= iy < ny and 0 <= ix < nx:
    iy_clipped = int(np.clip(iy, 1, ny - 2))
    ix_clipped = int(np.clip(ix, 1, nx - 2))
    bed_elev = float(dem[iy_clipped, ix_clipped])
    print(f"Bed elevation at breach: {bed_elev:.1f} m")
    print(f"Water surface elevation: {wse_m:.1f} m")
    print(f"Water depth at breach: {wse_m - bed_elev:.1f} m")
    print(f"Freeboard (WSE - bed): {(wse_m - bed_elev - 55.0):.1f} m")
    
    if wse_m > bed_elev:
        print(f"✓ Water surface is ABOVE ground - dam fills")
    else:
        print(f"✗ Water surface is BELOW ground - no impoundment!")
        
    # Check neighborhood
    neighborhood = dem[max(0, int(iy_clipped)-5):int(iy_clipped)+5, 
                       max(0, int(ix_clipped)-5):int(ix_clipped)+5]
    print(f"\n5x5 neighborhood around breach:")
    print(neighborhood)
else:
    print(f"✗ Breach ({iy}, {ix}) is OUTSIDE DEM bounds!")
