"""One command for "where does every dam actually stand".

Runs the real `validate_geometry` over every manifest in `data/geometry/` and
prints the verdict with the numbers behind it. For manifests that PASS, it also
locates **where** the upstream basin spills, which is the question G4's scalar
`spill_level_m` does not answer and which decides whether a G4 failure is
terrain or geometry:

  * escape far from the barrier  -> a genuine col in the valley rim. Terrain.
    Closing it means re-sourcing the impoundment, not changing code.
  * escape at the barrier        -> water going AROUND the footprint, which is
    a geometry defect and would be worth fixing.

Measured 2026-09-19: phutkal's escape is **1,069 m (38 cells) from its barrier**
with 238 of 293 rim cells below crest, so its G4 failure is terrain — proven
rather than reasoned. That question had been open since the gate was written.

Diagnostic only. Runs no solver, writes nothing, changes no verdict. This exists
so the next session starts from a command instead of re-deriving a day of
measurements by hand (findings_results.md, 2026-09-19).

Usage:  python scripts/diagnostics/check_geometry_gates.py [coarsen]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio import Affine
from scipy import ndimage
from shapely.geometry import Point, shape
from shapely.ops import transform as shp_transform
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.data_fetcher import scenario_key_for            # noqa: E402
from src.m2_geometry.dem_utils import condition_dem         # noqa: E402
from src.m2_geometry.validation import (                    # noqa: E402
    GeometryValidationError, validate_geometry)

GEOMETRY_DIR = ROOT / "data" / "geometry"
DEM_DIR = ROOT / "data" / "dem"

# An escape this close to the barrier is water going around the footprint rather
# than over a rim feature. Three cells, not a tuned number: two cells is the
# minimum wall the face-averaged bed can represent at all (validation.py's own
# note), so anything within three is indistinguishable from the structure.
_AT_BARRIER_CELLS = 3.0


def _escape_location(elevation, transform, result, manifest_geom_barrier):
    """Where the basin first spills, and how far that is from the barrier."""
    crest = float(result["crest_elev_m"])
    spill = float(result["spill_level_m"])
    barrier_mask = result["barrier_mask"]
    row, col = result["upstream_seed_rc"]

    emplaced = np.where(barrier_mask & (elevation < crest), crest, elevation)
    cross = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=bool)
    labels, _ = ndimage.label(emplaced <= spill + 1e-6, structure=cross)
    pool = labels == labels[row, col]
    rim = ndimage.binary_dilation(pool, structure=cross) & ~pool
    if not rim.any():
        return None
    rim_z = np.where(rim, emplaced, np.inf)
    rr, cc = np.unravel_index(int(np.argmin(rim_z)), rim_z.shape)
    x, y = rasterio.transform.xy(transform, rr, cc)
    return {
        "pool_cells": int(pool.sum()),
        "escape_elev_m": float(rim_z[rr, cc]),
        "distance_to_barrier_m": float(manifest_geom_barrier.distance(Point(x, y))),
        "rim_cells_below_crest": int((rim & (emplaced < crest)).sum()),
        "rim_cells": int(rim.sum()),
    }


def check(manifest_path: Path, coarsen: int) -> None:
    key = manifest_path.stem
    scenario = scenario_key_for(key)
    dem_path = DEM_DIR / f"{scenario}_dem.tif"
    print(f"{key}")
    if not dem_path.exists():
        print(f"    no DEM at {dem_path.name}")
        return

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with rasterio.open(dem_path) as src:
        raw, transform, crs, nodata = src.read(1), src.transform, src.crs, src.nodata
    elevation, _ = condition_dem(raw, nodata=nodata)
    if coarsen > 1:
        elevation = elevation[::coarsen, ::coarsen]
        transform = transform * Affine.scale(coarsen, coarsen)

    try:
        result = validate_geometry(manifest, elevation, transform, crs)
    except GeometryValidationError as exc:
        print(f"    REFUSED  {exc}")
        for field, value in (exc.result or {}).items():
            if field in ("cause", "barrier_span_m", "barrier_end_elev_m",
                         "barrier_ends_above_level", "water_level_m",
                         "wet_cells_below_level", "shared_component_cells"):
                print(f"        {field}: {value}")
        return

    crest = float(result["crest_elev_m"])
    spill = float(result["spill_level_m"])
    level = float(manifest["water_level_m"])
    g4 = spill >= level
    print(f"    GATE PASS   crest {crest:.2f} m   spill {spill:.2f} m   "
          f"G4 {'PASS' if g4 else f'FAIL by {level - spill:.2f} m'}")

    to_dem = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    by_role: dict[str, list] = {}
    for feature in manifest["geometry"]["features"]:
        by_role.setdefault(feature["properties"]["role"], []).append(
            shape(feature["geometry"]))
    barrier = shp_transform(to_dem, (by_role.get("dam_body") or by_role["blockage"])[0])

    escape = _escape_location(elevation, transform, result, barrier)
    if escape is None:
        return
    cells = escape["distance_to_barrier_m"] / abs(transform.a)
    where = ("AT the barrier — water is going AROUND the footprint, which is a "
             "GEOMETRY defect" if cells <= _AT_BARRIER_CELLS else
             "a genuine col in the valley rim, away from the structure — TERRAIN")
    print(f"    escape at {escape['escape_elev_m']:.2f} m, "
          f"{escape['distance_to_barrier_m']:.1f} m ({cells:.1f} cells) from the barrier")
    print(f"    pool at spill {escape['pool_cells']} cells; "
          f"{escape['rim_cells_below_crest']} of {escape['rim_cells']} rim cells below crest")
    print(f"    -> {where}")


def main() -> None:
    coarsen = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    print(f"geometry gate over every manifest (coarsen {coarsen})\n")
    for manifest_path in sorted(GEOMETRY_DIR.glob("*.json")):
        try:
            check(manifest_path, coarsen)
        except Exception as exc:                              # noqa: BLE001
            print(f"    ERROR {type(exc).__name__}: {exc}")
        print()


if __name__ == "__main__":
    main()
