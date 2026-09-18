"""Flood extent anchored to the REPORTED water depths, not to an arbitrary terrain stage.

    python scripts/observed_wse_extent.py            # -> data/admin/annamayya_observed_wse.geojson
    python scripts/observed_wse_extent.py --check    # analytic self-check, touches no data

WHY THIS EXISTS
---------------
The layer this replaces on the map (`annamayya_corridor.geojson`) is HAND at 2/5/10 m.
Nothing chose those stages; they are round numbers. Worse, the terrain is the same
Copernicus GLO-30 the solver integrates over, so agreement with it measures the DEM
against itself. It looked like evidence and was not.

Here the water *level* comes from the record instead. `data/observations/annamayya/
arrivals.json` carries reported high-water depths at three places along the Cheyyeru
(EVD-23 Mandapalli, EVD-25 Gundlur, EVD-26 Nandalur railway bridge). Sample the ground
at each of those places, add the reported depth, and you have three observed
water-surface elevations. Interpolate between them and intersect with the DEM.

The depth is over the ground AT THE PLACE NAMED, not over the channel bed -- see
station_anchors(). Getting that backwards is not cosmetic: it moved Gundlur's surface
by 6.8 m and put the village above its own reported flood.

WHAT IT STILL IS NOT
--------------------
Not an observation of the extent. The *level* is reported; the *shoreline* is still our
DEM, so the circularity is reduced and not removed. Never compute a CSI against it and
never serve it through the `observed` endpoint.

Not extrapolated. The reconstruction stops dead at the outer anchors -- roughly 8 km of
a ~40 km affected reach. EVD-28 at the Cheyyeru-Pennar confluence reports timing only,
so there is no level to carry the surface any further. The truncation is deliberate: a
map that stops where the evidence stops is honest; one that keeps going is not.

Not instrumental. `acquisition_proof.kind` on the source file is DOCUMENTARY -- revenue
registers, a railway emergency bulletin, eye-witness survey. A reported "ten feet" has
an error nobody ever quantified, which is why every anchor is a RANGE and the output is
a lo/hi envelope rather than a line.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

import numpy as np
import rasterio
import rasterio.features
import rasterio.windows
from rasterio.transform import xy as rio_xy
from scipy import ndimage
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import transform as shp_transform, unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEM_PATH = ROOT / "data" / "dem" / "annamayya_dem.tif"
ANCHORS_PATH = ROOT / "data" / "observations" / "annamayya" / "arrivals.json"
RIVERS_PATH = ROOT / "data" / "admin" / "annamayya_rivers.geojson"
OUT_PATH = ROOT / "data" / "admin" / "annamayya_observed_wse.geojson"

DAM_LONLAT = (79.02128, 14.21059)   # same anchor corridor_coverage.py uses
STEM_STEP_M = 15.0                  # stem densification, ~half a DEM cell
LATERAL_CAP_M = 5000.0              # beyond this, "nearest drainage" stops meaning anything
MIN_POLY_M2 = 1.0e4                 # drop 1 ha slivers left by the raster staircase
SIMPLIFY_M = 15.0


# -- inputs -------------------------------------------------------------------

def load_anchors():
    """The arrival records that carry BOTH a position and a reported depth."""
    doc = json.load(io.open(ANCHORS_PATH, encoding="utf-8"))
    out = []
    for r in doc.get("records", []):
        d = r.get("obs_depth_m_range")
        if not d or r.get("lat") is None or r.get("lon") is None:
            continue
        out.append({
            "id": r["id"], "name": r["name"], "lat": r["lat"], "lon": r["lon"],
            "depth_lo_m": float(d[0]), "depth_hi_m": float(d[1]),
            "evidence_class": r.get("evidence_class"), "source": r.get("source"),
            "source_hash": r.get("source_hash"),
        })
    return out, doc.get("acquisition_proof", {})


def load_stem(crs):
    """Downstream Cheyyeru main stem, oriented away from the dam, in `crs`.

    Same extraction corridor_coverage.front_along_stem uses. build_rivers is tried
    first because it is the path that knows about bbox-stale caches; it can block on
    an Overpass refetch, so a direct read of the cached layer is the fallback.
    """
    import geopandas as gpd
    from pyproj import Transformer

    note = ""
    try:
        from src.data_fetcher import build_rivers
        riv = build_rivers("annamayya")
        if riv.attrs.get("bbox_stale"):
            note = "river layer is BBOX-STALE for this domain"
        riv = riv.to_crs(crs)
    except Exception as exc:                                       # noqa: BLE001
        riv = gpd.read_file(RIVERS_PATH).to_crs(crs)
        note = "read cached river layer directly (" + str(exc)[:60] + ")"

    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    dam = Point(fwd.transform(*DAM_LONLAT))
    reaches = [g for n, g in zip(riv.get("name"), riv.geometry) if str(n) == "Cheyyeru"]
    if not reaches:
        raise SystemExit("no reach named Cheyyeru in the river layer")
    stem = min(reaches, key=lambda g: Point(g.coords[0]).distance(dam))
    if Point(stem.coords[-1]).distance(dam) < Point(stem.coords[0]).distance(dam):
        stem = LineString(list(stem.coords)[::-1])
    return stem, note


# -- the reconstruction -------------------------------------------------------

def ground_elevation(dem, transform, pt, win=1):
    """Ground under `pt`: local minimum over a (2*win+1)^2 window.

    GLO-30 is a *surface* model. A tree, a roof or a bridge deck in the cell reads
    metres high, and adding a reported depth to that puts the water surface up in
    the canopy. The corridor build hit the same thing and answered it the same way.
    """
    r, c = rasterio.transform.rowcol(transform, pt.x, pt.y)
    r0, r1 = max(0, r - win), min(dem.shape[0], r + win + 1)
    c0, c1 = max(0, c - win), min(dem.shape[1], c + win + 1)
    block = dem[r0:r1, c0:c1]
    block = block[np.isfinite(block)]
    if not block.size:
        raise SystemExit("no valid DEM under anchor at %.0f,%.0f" % (pt.x, pt.y))
    return float(block.min())


def station_anchors(dem, transform, stem, anchors, crs):
    """Turn each reported depth into a water-surface elevation, and station it.

    The level is taken at the REPORTED PLACE, not at the channel: a record saying
    "there was three metres of water in Gundlur" is three metres over Gundlur's
    ground. Anchoring to the channel bed instead is measurably wrong here and was
    the first thing this script got wrong -- Gundlur sits on a terrace 618 m off
    the Cheyyeru whose ground is 6.8 m above the bed, so bed + reported depth put
    the village 2.8 m ABOVE its own reported flood.

    Still water is level, so a surface elevation observed off-channel is the same
    surface elevation; the stem projection is only used to order and interpolate.

    `bed_elev_m` is kept alongside for the diagnostic contrast, not used for the
    level. ponytail: one rule for every anchor. It holds because the one genuinely
    in-channel record here (EVD-26, a railway bridge washout) sits 0.3 m above the
    bed anyway. An anchor later added as an explicit channel-gauge reading would
    need its own flag.
    """
    from pyproj import Transformer
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    out = []
    for a in anchors:
        p = Point(fwd.transform(a["lon"], a["lat"]))
        s = stem.project(p)
        ground = ground_elevation(dem, transform, p)
        bed = ground_elevation(dem, transform, stem.interpolate(s))
        out.append({**a,
                    "station_m": float(s),
                    "offset_from_stem_m": float(p.distance(stem)),
                    "ground_elev_m": ground,
                    "bed_elev_m": bed,
                    "wse_lo_m": ground + a["depth_lo_m"],
                    "wse_hi_m": ground + a["depth_hi_m"]})
    out.sort(key=lambda d: d["station_m"])
    return out


def slope_report(anchored, max_m_per_km=5.0):
    """Reach-average water-surface slope between consecutive anchors.

    Reported here rather than smoothed away. A steep segment means the two records
    bounding it are hard to reconcile with one still-water surface -- a coordinate
    sitting on a terrace, a depth measured against a different datum, or a genuinely
    steep wave front. Measured on this event, EVD-25 -> EVD-26 comes out near 11 m/km
    over 750 m against 1.9 m/km for the 8 km above it, and EVD-25's cited position is
    618 m off the channel on higher ground. The number is the finding; do not tune it.
    """
    out = []
    for a, b in zip(anchored, anchored[1:]):
        dl = (b["station_m"] - a["station_m"]) / 1000.0
        if dl <= 0:
            continue
        sl = (a["wse_lo_m"] - b["wse_lo_m"]) / dl
        out.append({"from": a["id"], "to": b["id"], "reach_km": round(dl, 2),
                    "slope_m_per_km": round(sl, 2), "steep": bool(sl > max_m_per_km)})
    return out


def monotonicity_report(anchored):
    """A water surface that RISES downstream is a red flag, not something to fix.

    It means an anchor coordinate, a reported depth, or the DEM bed under one of them
    is wrong. Silently sorting or clamping it would bury the contradiction inside a
    map that then looks fine. Report it and let the caller decide.
    """
    bad = []
    for k in ("wse_lo_m", "wse_hi_m"):
        for a, b in zip(anchored, anchored[1:]):
            if b[k] > a[k] + 1e-9:
                bad.append("%s: %s %.1f m -> %s %.1f m (+%.2f m over %.2f km)" % (
                    k, a["id"], a[k], b["id"], b[k], b[k] - a[k],
                    (b["station_m"] - a["station_m"]) / 1000.0))
    return bad


def reconstruct(dem, transform, stem, anchored,
                lateral_cap_m=LATERAL_CAP_M, step_m=STEM_STEP_M):
    """Boolean lo/hi extents where the interpolated observed water surface covers ground.

    Returns (wet_lo, wet_hi, diag). Cells are kept only when they are
      * inside the anchored span (no extrapolation past the outer anchors),
      * within `lateral_cap_m` of the stem, and
      * 8-connected to the channel itself.
    """
    s_lo = anchored[0]["station_m"]
    s_hi = anchored[-1]["station_m"]
    if s_hi - s_lo < step_m:
        raise SystemExit("anchors do not span a usable reach")

    stations = np.arange(s_lo, s_hi + step_m, step_m)
    pts = np.array([[p.x, p.y] for p in (stem.interpolate(s) for s in stations)])
    tree = cKDTree(pts)

    # Only the neighbourhood of the anchored reach can possibly be wet. Everything
    # outside it is skipped rather than queried -- the full grid is ~3.9 M cells.
    res = abs(transform.a)
    minx, miny = pts.min(axis=0) - lateral_cap_m
    maxx, maxy = pts.max(axis=0) + lateral_cap_m
    r_lo, c_lo = rasterio.transform.rowcol(transform, minx, maxy)
    r_hi, c_hi = rasterio.transform.rowcol(transform, maxx, miny)
    r_lo = int(np.clip(r_lo, 0, dem.shape[0] - 1)); r_hi = int(np.clip(r_hi + 1, 1, dem.shape[0]))
    c_lo = int(np.clip(c_lo, 0, dem.shape[1] - 1)); c_hi = int(np.clip(c_hi + 1, 1, dem.shape[1]))

    rows = np.arange(r_lo, r_hi)
    cols = np.arange(c_lo, c_hi)
    rr, cc = np.meshgrid(rows, cols, indexing="ij")
    xs, ys = rio_xy(transform, rr.ravel(), cc.ravel())
    cells = np.column_stack([np.asarray(xs), np.asarray(ys)])

    dist, idx = tree.query(cells, workers=-1)

    # The span guard. A cell whose nearest stem point is an END of the anchored reach
    # and which lies BEYOND that end is outside the evidence, however close it is.
    # Without this the last anchor's level leaks downstream through the lateral cap,
    # which is exactly the extrapolation this script refuses to do.
    #
    # The tolerance is one cell diagonal. Without it the guard cuts the outermost
    # ANCHOR's own cell: the cell centre sits up to half a diagonal from the cited
    # coordinate, which at the very first stem point is enough to project a few
    # metres upstream of the span and be rejected. Measured on EVD-23, whose ground
    # is 3.5 m below its own reported surface and was still coming back dry. One
    # cell against an 8.7 km reach is discretisation, not extrapolation.
    tol = res * np.sqrt(2.0)
    keep = dist <= lateral_cap_m
    for end, tangent in ((0, pts[0] - pts[1]), (len(pts) - 1, pts[-1] - pts[-2])):
        sel = idx == end
        if sel.any():
            v = cells[sel] - pts[end]
            keep[sel] &= (v @ tangent) / np.hypot(*tangent) <= tol

    sub = dem[r_lo:r_hi, c_lo:c_hi]
    shp = sub.shape
    station_of_cell = stations[idx]
    a_s = np.array([a["station_m"] for a in anchored])

    seed = np.zeros(shp, bool)
    srr, scc = rasterio.transform.rowcol(transform, pts[:, 0], pts[:, 1])
    srr = np.asarray(srr) - r_lo
    scc = np.asarray(scc) - c_lo
    ok = (srr >= 0) & (srr < shp[0]) & (scc >= 0) & (scc < shp[1])
    seed[srr[ok], scc[ok]] = True

    out = []
    for k in ("wse_lo_m", "wse_hi_m"):
        wse = np.interp(station_of_cell, a_s, np.array([a[k] for a in anchored]))
        wet = keep & np.isfinite(sub.ravel()) & (sub.ravel() < wse)
        wet = wet.reshape(shp)

        # Only water continuous with the channel. An isolated pit that happens to sit
        # below the interpolated surface was never connected to this flood.
        lab, _ = ndimage.label(wet, structure=np.ones((3, 3), int))
        live = np.unique(lab[seed & (lab > 0)])
        wet &= np.isin(lab, live[live > 0])
        out.append(wet)

    diag = {"window": (r_lo, r_hi, c_lo, c_hi), "res_m": res,
            "reach_km": (s_hi - s_lo) / 1000.0, "stem_points": len(pts)}
    return out[0], out[1], diag


# -- output -------------------------------------------------------------------

def polygonise(mask, transform, crs):
    geoms = [shape(g) for g, v in rasterio.features.shapes(
        mask.astype("uint8"), mask=mask, transform=transform) if v == 1]
    if not geoms:
        return None
    g = unary_union(geoms).buffer(0)
    parts = [p for p in (g.geoms if hasattr(g, "geoms") else [g]) if p.area >= MIN_POLY_M2]
    if not parts:
        return None
    g = unary_union(parts).simplify(SIMPLIFY_M)
    from pyproj import Transformer
    back = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
    return shp_transform(back, g), g.area


def build(out_path=OUT_PATH, lateral_cap_m=LATERAL_CAP_M):
    anchors, proof = load_anchors()
    if len(anchors) < 2:
        raise SystemExit("need >=2 depth anchors, found %d" % len(anchors))

    with rasterio.open(DEM_PATH) as src:
        dem = src.read(1).astype(float)
        dem[dem == src.nodata] = np.nan
        transform, crs = src.transform, src.crs

    stem, note = load_stem(crs)
    anchored = station_anchors(dem, transform, stem, anchors, crs)
    warn = monotonicity_report(anchored)
    slopes = slope_report(anchored)

    print("=" * 74)
    print("OBSERVED-ANCHORED WATER SURFACE -- Annamayya / Cheyyeru, 19 Nov 2021")
    print("=" * 74)
    if note:
        print("note: " + note)
    print("%-8s %-24s %8s %6s %8s %7s %8s %8s" % (
        "id", "place", "station", "off", "ground", "bed", "WSE lo", "WSE hi"))
    for a in anchored:
        print("%-8s %-24s %7.2fk %5.0fm %8.1f %7.1f %8.1f %8.1f" % (
            a["id"], a["name"][:24], a["station_m"] / 1000, a["offset_from_stem_m"],
            a["ground_elev_m"], a["bed_elev_m"], a["wse_lo_m"], a["wse_hi_m"]))
    print()
    for sl in slopes:
        print("  %-8s -> %-8s  %5.2f km  %6.2f m/km%s" % (
            sl["from"], sl["to"], sl["reach_km"], sl["slope_m_per_km"],
            "   <-- STEEP, records hard to reconcile" if sl["steep"] else ""))
    if warn:
        print("\n!! WATER SURFACE RISES DOWNSTREAM -- not corrected, see monotonicity_report():")
        for w in warn:
            print("   " + w)

    wet_lo, wet_hi, diag = reconstruct(dem, transform, stem, anchored, lateral_cap_m)
    r_lo, r_hi, c_lo, c_hi = diag["window"]
    win_tr = rasterio.windows.transform(
        rasterio.windows.Window(c_lo, r_lo, c_hi - c_lo, r_hi - r_lo), transform)

    feats = []
    areas = {}
    geoms = {}
    # hi first so the map draws the wide upper bound underneath and the tighter
    # lower bound on top of it -- the doubled fill is then the best-supported core.
    for band, mask in (("hi", wet_hi), ("lo", wet_lo)):
        got = polygonise(mask, win_tr, crs)
        if got is None:
            raise SystemExit("%s band is empty -- reconstruction produced no water" % band)
        geom, area = got
        areas[band] = area / 1e6
        geoms[band] = geom
        feats.append({
            "type": "Feature",
            "properties": {
                "band": band,
                "bound": "lower" if band == "lo" else "upper",
                "class": ("reported depth, LOW end of range" if band == "lo"
                          else "reported depth, HIGH end of range"),
                "area_km2": round(area / 1e6, 2),
            },
            "geometry": mapping(geom),
        })

    # The bands come from the same surface with lo <= hi everywhere, so the lower
    # bound must nest inside the upper. Measured, it very nearly does: simplify() runs
    # per band, which lets the lo boundary poke a few metres outside hi across ~0.015%
    # of its area. That is a drawing artefact, not a physical one, so absorb it -- but
    # bound it first, because a LARGE escape would mean the reconstruction, not the
    # simplifier, is wrong.
    escaped = geoms["lo"].difference(geoms["hi"]).area
    assert escaped <= 0.01 * geoms["lo"].area, (
        "lo band escapes hi by %.2f%% -- too much for a simplify artefact"
        % (100 * escaped / geoms["lo"].area))
    if escaped:
        geoms["hi"] = unary_union([geoms["hi"], geoms["lo"]])
        feats[0]["geometry"] = mapping(geoms["hi"])

    doc = {
        "type": "FeatureCollection",
        "metadata": {
            "layer": "observed_anchored_flood_extent",
            "provenance": "OBSERVED_ANCHORED",
            "not_observed": True,
            "level_is_observed": True,
            "extent_is_terrain": True,
            "method": (
                "Water-surface reconstruction from reported high-water depths. Each "
                "anchor's depth range is added to the ground AT THE REPORTED PLACE (3x3 "
                "local minimum of Copernicus GLO-30, to reject DSM canopy and roof "
                "spikes), giving an observed water-surface elevation -- a reported "
                "village depth is depth over that village's ground, not over the "
                "channel bed some hundreds of metres away. The surface is "
                "interpolated linearly along the OSM-mapped Cheyyeru main stem between "
                "anchors and intersected with the DEM. Kept only where 8-connected to "
                "the channel and within %.0f km of it. The low and high ends of the "
                "reported depth ranges are carried through separately, so the layer is "
                "an envelope, not a line." % (lateral_cap_m / 1000.0)),
            "what_is_observed": (
                "The WATER LEVEL. Three reported high-water depths along the Cheyyeru."),
            "what_is_not_observed": (
                "The SHORELINE. Where that level meets the ground is read off Copernicus "
                "GLO-30 -- the same DEM the model integrates over. Agreement with this "
                "layer is therefore NOT independent validation. Never compute a CSI "
                "against it and never serve it through the observed endpoint."),
            "no_extrapolation": (
                "Covers only the %.1f km of Cheyyeru between the outer anchors. No "
                "reported depth exists beyond them -- EVD-28 at the Cheyyeru-Pennar "
                "confluence gives arrival timing only -- so the surface is not carried "
                "further. The affected reach is much longer than this. The layer stops "
                "where the evidence stops." % diag["reach_km"]),
            "acquisition": proof.get("statement"),
            "acquisition_kind": proof.get("kind"),
            "anchors": [
                {k: a[k] for k in ("id", "name", "lat", "lon", "depth_lo_m", "depth_hi_m",
                                   "ground_elev_m", "bed_elev_m", "offset_from_stem_m",
                                   "wse_lo_m", "wse_hi_m", "station_m",
                                   "evidence_class", "source", "source_hash")}
                for a in anchored],
            "monotonicity_warnings": warn,
            "surface_slope_between_anchors": slopes,
            "reach_km": round(diag["reach_km"], 2),
            "lateral_cap_m": lateral_cap_m,
            "dem": "Copernicus GLO-30 (data/dem/annamayya_dem.json for source URLs and hash)",
            "event": "Annamayya dam breach, 19 Nov 2021, Cheyyeru river",
            "crs": "EPSG:4326",
        },
        "features": feats,
    }
    out_path.write_text(json.dumps(doc), encoding="utf-8")

    print("\nreach anchored            %8.2f km" % diag["reach_km"])
    print("extent, reported depth lo %8.2f km2" % areas["lo"])
    print("extent, reported depth hi %8.2f km2" % areas["hi"])
    print("\nwrote %s" % out_path.relative_to(ROOT))
    return doc, anchored, areas


# -- self-check ---------------------------------------------------------------

def _check():
    """Analytic case: straight channel, constant side slope, known answer.

    Valley cross-section is a V of side slope m (rise/run) and the bed falls at slope
    b downstream. Anchored at both ends with an exact depth d, the reconstructed water
    surface must reproduce d at every station, so the wetted half-width is d/m and the
    total width 2d/m. That pins the interpolation, the bed sampling and the masking
    together -- if any of them is off, the width is wrong.
    """
    from affine import Affine
    res, n = 10.0, 200
    m, b, d = 0.05, 0.002, 4.0
    rows = np.arange(n)[:, None]
    cols = np.arange(n)[None, :]
    centre = n // 2
    dem = ((100.0 - b * res * rows) + m * res * np.abs(cols - centre)).astype(float)
    transform = Affine(res, 0, 0, 0, -res, 0)

    x_mid = (centre + 0.5) * res
    stem = LineString([(x_mid, -0.5 * res), (x_mid, -(n - 0.5) * res)])
    anchored = []
    for r in (10, n - 11):
        y = -(r + 0.5) * res
        s = stem.project(Point(x_mid, y))
        bed = ground_elevation(dem, transform, Point(x_mid, y))
        anchored.append({"id": "A%d" % r, "name": "a%d" % r, "station_m": float(s),
                         "bed_elev_m": bed, "wse_lo_m": bed + d, "wse_hi_m": bed + d})
    anchored.sort(key=lambda a: a["station_m"])

    assert not monotonicity_report(anchored), "synthetic surface should fall downstream"

    lo, hi, diag = reconstruct(dem, transform, stem, anchored,
                               lateral_cap_m=500.0, step_m=res / 2)
    r0, r1, c0, c1 = diag["window"]
    assert (lo == hi).all(), "identical depth bounds must give identical bands"

    mid = (10 + (n - 11)) // 2 - r0
    width = lo[mid].sum() * res
    assert abs(width - 2 * d / m) <= 2 * res, "width %s vs analytic %s" % (width, 2 * d / m)

    wet_rows = np.nonzero(lo.any(axis=1))[0] + r0
    assert wet_rows.min() >= 10 - 1, "leaked upstream of the first anchor: %d" % wet_rows.min()
    assert wet_rows.max() <= n - 11 + 1, "leaked past the last anchor: %d" % wet_rows.max()

    # An isolated pit below the surface but walled off from the channel stays dry.
    # Column 60 is inside the 500 m lateral window yet 40 cells out from the channel,
    # where the valley wall stands 20 m above the bed -- so it is unambiguously dry
    # terrain that only becomes a candidate because the pit is punched into it.
    pit_col = 60
    dem2 = dem.copy()
    dem2[mid + r0, pit_col:pit_col + 4] = 0.0
    lo2, _, d2 = reconstruct(dem2, transform, stem, anchored,
                             lateral_cap_m=500.0, step_m=res / 2)
    pit_in_win = pit_col - d2["window"][2]
    assert 0 <= pit_in_win < lo2.shape[1], "pit fell outside the reconstruction window"
    assert not lo2[mid, pit_in_win], "disconnected pit must not be filled"

    print("self-check OK")
    print("  reconstructed width %.0f m vs analytic %.0f m" % (width, 2 * d / m))
    print("  span honoured: wet rows %d..%d of anchors 10..%d" % (
        wet_rows.min(), wet_rows.max(), n - 11))
    print("  disconnected pit stayed dry")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="run the analytic self-check and exit")
    ap.add_argument("--lateral-cap-m", type=float, default=LATERAL_CAP_M)
    a = ap.parse_args()
    if a.check:
        _check()
    else:
        build(lateral_cap_m=a.lateral_cap_m)
