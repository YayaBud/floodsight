"""Author geometry manifests for scenarios that do not have one yet.

Computes every feature the same way the existing manifests (rishiganga.json,
phutkal.json, annamayya.json) were built: DEM ridge search for the barrier,
thalweg-snapped breach point, buffer for the breach zone, OSM river clipped to
the AOI, and upstream/downstream seeds walked along that river clear of the
barrier. Then runs the real `validate_geometry` against the conditioned DEM
and records the actual verdict -- never a hand-set `validated: true`.

Usage
-----
    python scripts/generate_geometry_manifest.py derna malpasset ivanovo
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import rasterio
import rasterio.transform as rtransform
from pyproj import Transformer
from shapely.geometry import LineString, Point, Polygon, mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data_fetcher import SCENARIOS
from src.m2_geometry.dem_utils import condition_dem, snap_to_thalweg
from src.m2_geometry.seed_walk import walk_seed
from src.m2_geometry.validation import GeometryValidationError, validate_geometry

DEM_DIR = ROOT / "data" / "dem"
ADMIN_DIR = ROOT / "data" / "admin"
GEOMETRY_DIR = ROOT / "data" / "geometry"

_RIDGE_WINDOW_M = 200.0        # matches existing manifests' "+-200 m" wording
_BREACH_BUFFER_M = 58.0        # within the existing files' ~55-61 m range
_SEED_WALK_MARGIN_M = 20.0     # extra clearance past the barrier polygon edge


def _load_dem(scenario_key: str):
    dem_path = DEM_DIR / f"{scenario_key}_dem.tif"
    with rasterio.open(dem_path) as ds:
        raw = ds.read(1)
        transform = ds.transform
        crs = ds.crs
        nodata = ds.nodata
    dem, _ = condition_dem(raw, nodata=nodata)
    return dem, transform, crs, dem_path


def _ridge_polygon(dem: np.ndarray, transform, x: float, y: float,
                    axis_dir: np.ndarray) -> tuple[list[tuple[float, float]], float, tuple[float, float]]:
    """DEM ridge search perpendicular to the river at the breach point.

    Samples the local elevation maximum in a +-_RIDGE_WINDOW_M window around
    the breach point -- the same windowing `run_pipeline.py` uses for
    `dem_crest_along_axis` -- then builds the barrier footprint as a fixed
    +-_RIDGE_WINDOW_M rectangle centred on the breach point, oriented
    perpendicular to the local river direction, at that ridge elevation. This
    matches the existing manifests' documented method literally (their
    `source` strings read "+-200 m", a fixed window, not a search that grows
    to whatever the local floodplain happens to need) and their measured
    footprint size (rishiganga.json: ~245 x 452 m). A data-driven "grow until
    the DEM clears water_level_m" variant was tried and rejected here: on
    malpasset/ivanovo's wider floodplain segments it grew the rectangle to
    1-2 km, at which point the barrier consumes the resolved breach_zone
    entirely and the breach-opening/downstream connectivity check becomes
    meaningless. A fixed, modest, physically-plausible footprint and letting
    `validate_geometry` fail honestly if 30 m terrain does not confine it is
    more defensible than growing the barrier until the check passes.
    """
    dy_m = abs(transform.e)
    dx_m = abs(transform.a)
    ny, nx = dem.shape
    wcol, wrow = ~transform * (x, y)
    r0 = max(0, int(wrow - _RIDGE_WINDOW_M / dy_m))
    r1 = min(ny, int(wrow + _RIDGE_WINDOW_M / dy_m) + 1)
    c0 = max(0, int(wcol - _RIDGE_WINDOW_M / dx_m))
    c1 = min(nx, int(wcol + _RIDGE_WINDOW_M / dx_m) + 1)
    ridge_elev = float(dem[r0:r1, c0:c1].max())

    # Perpendicular to the along-river direction, in the DEM's projected CRS.
    along = axis_dir / (np.linalg.norm(axis_dir) or 1.0)
    perp = np.array([-along[1], along[0]])

    half_len = _RIDGE_WINDOW_M           # rectangle half-length along the ridge
    half_wid = _RIDGE_WINDOW_M / 4.0     # rectangle half-width along the river
    center = np.array([x, y])
    corners = [
        center + perp * half_len + along * half_wid,
        center + perp * half_len - along * half_wid,
        center - perp * half_len - along * half_wid,
        center - perp * half_len + along * half_wid,
    ]
    ring = [(float(p[0]), float(p[1])) for p in corners]
    ring.append(ring[0])
    return ring, ridge_elev, (float(center[0] + along[0] * half_len),
                              float(center[1] + along[1] * half_len))


_CHAIN_SNAP_TOL_M = 60.0  # OSM ways are commonly split at tags/junctions with a small node gap

# Below this, a chain that dead-ends near the breach in one direction is
# bridged to the nearest remaining fragment rather than accepted as-is (see
# _clip_nearest_river's bridging pass). Distinct from _CHAIN_SNAP_TOL_M: that
# tolerance closes small digitisation gaps everywhere; this one only fires
# when the normal chain leaves a seed-walk direction with no usable river at
# all, and the bridge it adds is provenance-tagged as a reconstruction, never
# folded into the OBSERVED OSM line silently.
_BRIDGE_MAX_GAP_M = 700.0
_MIN_WALKABLE_M = 150.0  # a seed walk needs at least this much chain to clear a barrier


def _chain_from_seed(segs: list[LineString], i0: int) -> tuple[list[tuple[float, float]], set[int]]:
    used = {i0}
    chain = list(segs[i0].coords)
    changed = True
    while changed:
        changed = False
        head, tail = Point(chain[0]), Point(chain[-1])
        for i, seg in enumerate(segs):
            if i in used:
                continue
            p0, p1 = Point(seg.coords[0]), Point(seg.coords[-1])
            if p0.distance(tail) <= _CHAIN_SNAP_TOL_M:
                chain += list(seg.coords)[1:]
            elif p1.distance(tail) <= _CHAIN_SNAP_TOL_M:
                chain += list(reversed(seg.coords))[1:]
            elif p1.distance(head) <= _CHAIN_SNAP_TOL_M:
                chain = list(seg.coords)[:-1] + chain
            elif p0.distance(head) <= _CHAIN_SNAP_TOL_M:
                chain = list(reversed(seg.coords))[:-1] + chain
            else:
                continue
            used.add(i)
            changed = True
            break
    return chain, used


def _clip_nearest_river(scenario_key: str, crs, breach_xy: tuple[float, float]) -> tuple[LineString, bool]:
    """The OSM waterway line passing nearest the breach point.

    OSM rivers are mapped as many short ways split at tag/junction
    boundaries, not one line per watercourse (`data/admin/*_rivers.geojson`
    for these three scenarios has 95-309 separate LineStrings). Picking only
    the single nearest raw segment can be too short to walk a seed clear of
    the barrier in one direction. This stitches the segment nearest the
    breach point together with any other segment whose endpoint lies within
    `_CHAIN_SNAP_TOL_M` of the growing chain's own endpoint, in both
    directions, so "the one passing nearest the breach point" (the spec's
    wording) becomes one continuous walkable line along that watercourse.

    Bridging. A watercourse can dead-end right where OSM mappers stopped
    tagging it as `waterway=river` (commonly where it widens into a
    reservoir/dam pool) — confirmed for Derna: the chained line's far end
    sits within 0.3 m of the breach point with zero further OSM geometry for
    ~608 m in that direction, while ~5 km of continuous line is available the
    other way. That is a real mapping gap immediately at the structure, not
    absence of a river. If the natural chain leaves either seed-walk
    direction with less than `_MIN_WALKABLE_M` of line before its terminal
    vertex, the nearest other fragment's endpoint within `_BRIDGE_MAX_GAP_M`
    is spliced on with a straight connecting segment. The bridge span is
    reported back to the caller (as a boolean) so it can be provenance-tagged
    distinctly from the OBSERVED OSM geometry, never presented as if OSM
    mapped it.
    """
    gdf = gpd.read_file(ADMIN_DIR / f"{scenario_key}_rivers.geojson")
    if gdf.empty:
        raise ValueError(f"no OSM waterway available for '{scenario_key}'")
    gdf_proj = gdf.to_crs(crs)
    segs: list[LineString] = []
    for geom in gdf_proj.geometry.values:
        if geom is None or geom.is_empty:
            continue
        parts = geom.geoms if geom.geom_type == "MultiLineString" else [geom]
        segs.extend(parts)
    if not segs:
        raise ValueError(f"no usable waterway geometry for '{scenario_key}'")

    bp = Point(breach_xy)
    i0 = min(range(len(segs)), key=lambda i: segs[i].distance(bp))
    chain, used = _chain_from_seed(segs, i0)

    bridged = False
    proj_d = LineString(chain).project(bp)
    chain_len = LineString(chain).length
    room_forward = chain_len - proj_d
    room_backward = proj_d
    for room, extend_at_end in ((room_forward, True), (room_backward, False)):
        if room >= _MIN_WALKABLE_M:
            continue
        anchor = Point(chain[-1] if extend_at_end else chain[0])
        candidates = [(i, min(Point(seg.coords[0]).distance(anchor), Point(seg.coords[-1]).distance(anchor)))
                      for i, seg in enumerate(segs) if i not in used]
        if not candidates:
            continue
        j, gap = min(candidates, key=lambda t: t[1])
        if gap > _BRIDGE_MAX_GAP_M:
            continue
        seg = segs[j]
        p0, p1 = Point(seg.coords[0]), Point(seg.coords[-1])
        far_end_first = p1.distance(anchor) < p0.distance(anchor)
        bridge_coords = list(reversed(seg.coords)) if far_end_first else list(seg.coords)
        if extend_at_end:
            chain = chain + bridge_coords
        else:
            chain = list(reversed(bridge_coords)) + chain
        used.add(j)
        bridged = True

    return LineString(chain), bridged


def _snap_to_river_then_thalweg(dem, transform, river: LineString, x: float, y: float):
    """Put the breach on the river, then resolve it to the valley floor.

    Same two-step intent `run_pipeline.py` uses for scenarios without an
    explicit breach centerline: snap onto the OSM river line first (corrects
    the configured lon/lat to the mapped watercourse), then search a short
    radius for the local DEM thalweg (corrects DEM-vs-OSM misregistration).
    The thalweg search can walk a little off the OSM line's exact centreline
    on a coarse DEM, so the final point is re-projected onto the river line
    (nearest point) -- this is what `validate_geometry` requires ("breach
    point is not resolved on river at DEM resolution") and what the existing
    manifests' breach points already satisfy (rishiganga.json: ~7 m from its
    river feature, well inside one DEM cell).
    """
    bp = Point(x, y)
    on_river = river.interpolate(river.project(bp))
    fx, fy, felev, _ = snap_to_thalweg(dem, transform, on_river.x, on_river.y,
                                        search_radius_m=300.0)
    final = river.interpolate(river.project(Point(fx, fy)))
    frow, fcol = rtransform.rowcol(transform, final.x, final.y)
    final_elev = float(dem[int(frow), int(fcol)])
    total_moved = float(Point(x, y).distance(final))
    return float(final.x), float(final.y), final_elev, total_moved


def generate_geometry_manifest(scenario_key: str) -> dict[str, Any]:
    cfg = SCENARIOS[scenario_key]
    dem, transform, crs, dem_path = _load_dem(scenario_key)

    to_dem = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    to_wgs84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)

    raw_x, raw_y = to_dem.transform(cfg["breach_lon"], cfg["breach_lat"])
    river, river_bridged = _clip_nearest_river(scenario_key, crs, (raw_x, raw_y))

    # The OSM extract is fetched for the SCENARIOS bbox, which is not the same
    # rectangle as the DEM that ends up on disk -- annamayya's river ran 7.7 km
    # north of its DEM's top edge, and `validate_geometry` rejected the whole
    # manifest with "river lies outside DEM coverage". Clip to what the solver
    # can actually see, then keep the piece that still carries the breach.
    from shapely.geometry import box as _box
    from shapely.ops import linemerge as _linemerge, unary_union as _unary_union
    _dem_box = _box(transform.c, transform.f + transform.e * dem.shape[0],
                    transform.c + transform.a * dem.shape[1], transform.f)
    if not _dem_box.contains(river):
        _clipped = river.intersection(_dem_box.buffer(-abs(transform.a)))
        if _clipped.is_empty:
            raise ValueError(f"{scenario_key}: river does not intersect its DEM")
        if _clipped.geom_type != "LineString":
            _merged = _linemerge(_unary_union(_clipped))
            _parts = ([_merged] if _merged.geom_type == "LineString"
                      else list(_merged.geoms))
            _bp = Point(raw_x, raw_y)
            _clipped = min(_parts, key=lambda g: g.distance(_bp))
        print(f"  river clipped to DEM coverage: {river.length/1000:.2f} km "
              f"-> {_clipped.length/1000:.2f} km")
        river = _clipped
    snap_x, snap_y, snap_elev, moved_m = _snap_to_river_then_thalweg(
        dem, transform, river, raw_x, raw_y)
    # Local along-river direction at the breach point, for the perpendicular
    # ridge rectangle and for telling upstream from downstream along the line.
    proj_d = river.project(Point(snap_x, snap_y))
    d0 = max(0.0, proj_d - 25.0)
    d1 = min(river.length, proj_d + 25.0)
    p0, p1 = river.interpolate(d0), river.interpolate(d1)
    axis_dir = np.array([p1.x - p0.x, p1.y - p0.y])
    if np.linalg.norm(axis_dir) < 1e-6:
        axis_dir = np.array([1.0, 0.0])

    dam_height_m = float(cfg.get("dam_height_m", 55.0))
    water_level_m = float(cfg["wse_m"])

    barrier_ring, ridge_elev, crest_xy = _ridge_polygon(dem, transform, snap_x, snap_y, axis_dir)

    pixel_m = max(abs(transform.a), abs(transform.e))
    barrier_clear = Polygon(barrier_ring).buffer(pixel_m)
    upstream_xy = walk_seed(river, (snap_x, snap_y), barrier_clear, direction=+1)
    downstream_xy = walk_seed(river, (snap_x, snap_y), barrier_clear, direction=-1)

    def to4326(xy):
        lon, lat = to_wgs84.transform(xy[0], xy[1])
        return (lon, lat)

    def ring_to4326(ring):
        return [list(to4326(pt)) for pt in ring]

    breach_pt_4326 = to4326((snap_x, snap_y))
    crest_pt_4326 = to4326(crest_xy)

    breach_zone_poly = Point(snap_x, snap_y).buffer(_BREACH_BUFFER_M, resolution=32)
    breach_zone_ring = ring_to4326(list(breach_zone_poly.exterior.coords))

    river_coords_4326 = [list(to4326((x, y))) for x, y in river.coords]

    features = [
        {
            "type": "Feature",
            "properties": {
                "role": "dam_body",
                "classification": "MODEL_RECONSTRUCTION",
                "source": (
                    f"DEM ridge search perpendicular to the river at the breach point "
                    f"(+-{_RIDGE_WINDOW_M:.0f} m) on {scenario_key}_dem.tif, rendered as a "
                    f"buffered rectangle along the local perpendicular-to-river direction at "
                    f"the ridge elevation ({ridge_elev:.1f} m) — not a surveyed structure footprint"
                ),
                "height_m": dam_height_m,
            },
            "geometry": {"type": "Polygon", "coordinates": [ring_to4326(barrier_ring)]},
        },
        {
            "type": "Feature",
            "properties": {
                "role": "dam_axis",
                "classification": "MODEL_RECONSTRUCTION",
                "source": f"line from resolved breach point to local DEM crest, {scenario_key}_dem.tif",
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [list(breach_pt_4326),
                                 list(to4326(((snap_x + crest_xy[0]) / 2, (snap_y + crest_xy[1]) / 2))),
                                 list(crest_pt_4326)],
            },
        },
        {
            "type": "Feature",
            "properties": {
                "role": "breach_point",
                "classification": "RECONSTRUCTED",
                "source": (
                    f"src/data_fetcher.py SCENARIOS breach_lon/breach_lat, snapped to the "
                    f"DEM thalweg (moved {moved_m:.0f} m)"
                ),
            },
            "geometry": {"type": "Point", "coordinates": list(breach_pt_4326)},
        },
        {
            "type": "Feature",
            "properties": {
                "role": "breach_zone",
                "classification": "MODEL_RECONSTRUCTION",
                "source": f"buffer({_BREACH_BUFFER_M:.0f} m) around the resolved breach point",
            },
            "geometry": {"type": "Polygon", "coordinates": [breach_zone_ring]},
        },
        {
            "type": "Feature",
            "properties": {
                "role": "river",
                "classification": "MODEL_RECONSTRUCTION" if river_bridged else "OBSERVED",
                "source": (
                    f"OpenStreetMap waterway, data/admin/{scenario_key}_rivers.geojson, "
                    f"bridged across an OSM coverage gap near the breach with a straight "
                    f"connecting segment (classification MODEL_RECONSTRUCTION applies to "
                    f"the bridged span only; the rest of the line is OSM-observed)"
                ) if river_bridged else f"OpenStreetMap waterway, data/admin/{scenario_key}_rivers.geojson",
            },
            "geometry": {"type": "LineString", "coordinates": river_coords_4326},
        },
        {
            "type": "Feature",
            "properties": {
                "role": "upstream_seed",
                "classification": "MODEL_RECONSTRUCTION",
                "source": "nearest river point upstream of the breach clear of the barrier (walked from the OSM line)",
            },
            "geometry": {"type": "Point", "coordinates": list(to4326(upstream_xy))},
        },
        {
            "type": "Feature",
            "properties": {
                "role": "downstream_seed",
                "classification": "MODEL_RECONSTRUCTION",
                "source": "nearest river point downstream of the breach clear of the barrier (walked from the OSM line)",
            },
            "geometry": {"type": "Point", "coordinates": list(to4326(downstream_xy))},
        },
    ]

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "scenario_key": scenario_key,
        "vertical_datum": "EGM2008",
        "dem_vertical_datum": "EGM2008",
        "water_level_m": water_level_m,
        "geometry": {"type": "FeatureCollection", "features": features},
    }

    try:
        validate_geometry(manifest, dem, transform, crs)
        manifest["validated"] = True
        manifest["validation_reason"] = None
    except GeometryValidationError as exc:
        manifest["validated"] = False
        manifest["validation_reason"] = str(exc)

    return manifest


def main(argv: list[str]) -> int:
    keys = argv or ["derna", "malpasset", "ivanovo"]
    for key in keys:
        manifest = generate_geometry_manifest(key)
        out_path = GEOMETRY_DIR / f"{key}.json"
        out_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        status = "VALID" if manifest["validated"] else f"INVALID: {manifest['validation_reason']}"
        print(f"{key}: {status} -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
