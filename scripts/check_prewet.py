"""Which validation points were ALREADY wet before the dam broke?

The wet-channel run starts from an 18 h spin-up that left 51.84 MCM standing over
57.8 km2. Any EVD point inside that footprint reports an "arrival" that is really the
initial condition, not a wave. Arrival numbers from that run are only readable for
points this script reports as DRY at t=0.
"""
import sys
import glob
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer

ROOT = Path(r"D:\sih_work\floodsight")
sys.path.insert(0, str(ROOT))

RUN = ROOT / "data" / "scenarios" / "annamayya_stage2_wetchannel"
WET = 0.30

# frame 0 of the main run IS the initial condition handed over by the spin-up
frames = sorted(glob.glob(str(RUN / "depth_rasters" / "depth_*.tif")))
if not frames:
    raise SystemExit("no frames yet")
with rasterio.open(frames[0]) as s:
    h0 = s.read(1).astype(float)
    tr, crs = s.transform, s.crs
dx, dy = abs(tr.a), abs(tr.e)

print(f"initial condition (frame 0): {float(h0.sum()*dx*dy)/1e6:.2f} MCM standing, "
      f"{int((h0 > WET).sum())} cells > {WET} m = {(h0>WET).sum()*dx*dy/1e6:.1f} km2, "
      f"max {h0.max():.2f} m")

POINTS = [
    ("EVD-23 Mandapalli", 79.0412, 14.2480),
    ("EVD-24 Pulapathur", 79.0445, 14.2473),
    ("EVD-25 Gundlur", 79.1140, 14.2522),
    ("EVD-26 Nandalur bridge", 79.1200, 14.2580),
    ("EVD-28 Pennar confluence", 79.1699, 14.4311),
]
fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
rad = max(1, int(300.0 / dx))
print(f"\n{'point':26} {'h0 peak m':>10}  verdict")
for name, lon, lat in POINTS:
    x, y = fwd.transform(lon, lat)
    r, c = rasterio.transform.rowcol(tr, x, y)
    r0, r1 = max(0, r - rad), min(h0.shape[0], r + rad + 1)
    c0, c1 = max(0, c - rad), min(h0.shape[1], c + rad + 1)
    peak = float(np.nanmax(h0[r0:r1, c0:c1])) if r1 > r0 and c1 > c0 else float("nan")
    verdict = ("PRE-WET — its arrival is the initial condition, NOT a wave"
               if peak > WET else "dry at t=0 — arrival is readable")
    print(f"{name:26} {peak:10.2f}  {verdict}")
