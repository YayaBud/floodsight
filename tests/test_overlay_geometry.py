"""
The map overlays must not expose the solver's square cells.

Both overlay paths vectorise a raster, and rasterio.features.shapes puts every
vertex on a raster grid line. That produces two independent artefacts:

* A staircase. Its signature is perimeter: a cell-edge path along a diagonal is
  the taxicab distance, up to sqrt(2) longer than the line it traces. So
  `perimeter / perimeter after simplifying at 2 cells` is ~1.38 for raw
  vectoriser output and ~1.00 for a boundary that is genuinely smooth.

  Vertex angles do *not* work as the metric here. A buffer(+r).buffer(-r)
  closing replaces each 90-degree corner with a short arc, which drives the
  right-angle count to 0% while leaving the boundary just as staircased -- that
  is exactly how an earlier attempt at this fix measured as smooth while the
  map still showed squares. Perimeter excess sees through it.

* Fragmentation. The fringe of any agreement raster is scattered lone cells,
  and each vectorises into its own cell-sized blob. Rounding their corners does
  not help: 300 rounded specks read as grid noise just as much as 300 squares.
  Only the sub-polygon count catches this.

Every bound below is a measured value, cited at the assertion, and every metric
is paired with a control that must fail -- so a metric that stops
discriminating fails loudly instead of passing vacuously.
"""
import numpy as np
from affine import Affine
from pyproj import Transformer
from rasterio.features import shapes
from scipy import ndimage
from shapely.geometry import shape
from shapely.ops import transform as shp_transform, unary_union

from run_pipeline import _depth_to_geojson, THRESHOLDS
from src.m10_validation.observed import (
    ExtentComparison, agreement_geojson, AGREE_HIT, AGREE_MISS, AGREE_FALSE,
    SOURCES,
)

CELL = 30.0
CRS = "EPSG:32633"
TRANSFORM = Affine(CELL, 0, 500000, 0, -CELL, 3600000)
CELL_AREA = CELL * CELL
_to_utm = Transformer.from_crs("EPSG:4326", CRS, always_xy=True).transform


# --- metrics ----------------------------------------------------------------

def perimeter_excess(geoms):
    """Boundary length over its own length simplified at 2 cells.

    Geometries must be in metres, so WGS84 overlay output is reprojected first.
    """
    per = sum(g.length for g in geoms)
    smooth = sum(g.simplify(2 * CELL).length for g in geoms)
    assert smooth > 0, "no boundary produced"
    return per / smooth


def n_vertices(geoms):
    return sum(len(np.asarray(r.coords))
               for g in geoms for p in getattr(g, "geoms", [g])
               for r in [p.exterior, *p.interiors])


def n_parts(geom):
    return len(getattr(geom, "geoms", [geom]))


def to_utm(geojson_geom):
    return shp_transform(_to_utm, shape(geojson_geom))


# --- fixtures ---------------------------------------------------------------

def diagonal_depth(n=120, speckle=False, seed=1):
    """A diagonal wet band -- the worst case for staircase artefacts.

    With speckle=True, isolated barely-wet cells are scattered outside the
    band. A real max-depth grid has these at the flood fringe, and each one
    vectorises into its own cell-sized square unless the blur removes it.
    """
    yy, xx = np.mgrid[0:n, 0:n]
    d = 6.0 - 0.9 * np.abs(0.7 * xx - yy + 12) / 3.0
    d += 0.4 * np.sin(xx / 7.0) * np.cos(yy / 9.0)
    d = np.maximum(d, 0.0).astype(np.float32)
    if speckle:
        rng = np.random.default_rng(seed)
        lone = (rng.random((n, n)) < 0.015) & (d <= 0.0)
        d[lone] = 0.45
    return d


def agreement_grid(speckle=True, n=120, seed=0):
    """Three agreement bands, optionally with the usual lone-cell fringe."""
    yy, xx = np.mgrid[0:n, 0:n]
    band = 0.7 * xx - yy + 12
    a = np.zeros((n, n), dtype=np.int16)
    a[np.abs(band) < 14] = AGREE_HIT
    a[(band >= 14) & (band < 22)] = AGREE_FALSE
    a[(band <= -14) & (band > -22)] = AGREE_MISS
    if speckle:
        rng = np.random.default_rng(seed)
        for code in (AGREE_HIT, AGREE_MISS, AGREE_FALSE):
            a[(rng.random((n, n)) < 0.012) & (a == 0)] = code
    return a


def comparison(agree):
    return ExtentComparison(
        skill=None, agreement=agree, transform=TRANSFORM, crs=CRS,
        cell_area_m2=CELL_AREA, threshold_m=0.3, source=SOURCES["derna"],
        overlapped=True,
    )


def vectorise_raw(mask, connectivity=4):
    """What the unsmoothed vectoriser produces, for use as a control."""
    return unary_union([shape(g) for g, v in shapes(
        mask.astype(np.uint8), mask=mask, transform=TRANSFORM,
        connectivity=connectivity) if v == 1])


def connected_area_m2(mask, min_cells=4):
    """Area in components of at least min_cells -- i.e. excluding lone specks."""
    lbl, k = ndimage.label(mask)
    if not k:
        return 0.0
    sizes = ndimage.sum(np.ones_like(lbl), lbl, range(1, k + 1))
    return float(sum(s for s in sizes if s >= min_cells)) * CELL_AREA


# --- controls: these prove the metrics can fail ------------------------------

def test_control_unsmoothed_boundary_has_high_perimeter_excess():
    raw = vectorise_raw(diagonal_depth() >= THRESHOLDS[0])
    assert perimeter_excess([raw]) > 1.25              # measured 1.378


def test_control_unsmoothed_speckle_fragments():
    """Lone cells each become their own polygon if nothing removes them."""
    raw = vectorise_raw(agreement_grid() == AGREE_MISS, connectivity=8)
    assert n_parts(raw) > 50                           # measured 100+


# --- depth overlay ----------------------------------------------------------

def depth_overlay():
    gj = _depth_to_geojson(diagonal_depth(), TRANSFORM, 0.0, lambda x, y: (x, y))
    assert gj["features"]
    return gj


def test_depth_overlay_is_not_cell_shaped():
    geoms = [shape(f["geometry"]) for f in depth_overlay()["features"]]
    excess = perimeter_excess(geoms)
    assert excess < 1.03, (                            # measured 1.005
        "depth overlay still staircased: perimeter excess %.3f" % excess)


def test_depth_overlay_is_not_padded_with_arc_vertices():
    """A buffer-based smoother inflates the payload without fixing the shape."""
    nv = n_vertices([shape(f["geometry"]) for f in depth_overlay()["features"]])
    assert nv < 3000, "depth overlay carries %d vertices" % nv   # measured 193


def test_depth_overlay_preserves_wet_area():
    """Smoothing is display-only: it must not shrink the reported flood."""
    depth = diagonal_depth()
    gj = _depth_to_geojson(depth, TRANSFORM, 0.0, lambda x, y: (x, y))
    raw = (depth >= THRESHOLDS[0]).sum() * CELL_AREA
    poly = sum(shape(f["geometry"]).area for f in gj["features"])
    assert abs(poly - raw) / raw < 0.05                # measured +1.4%


# --- agreement overlay ------------------------------------------------------

def agreement_overlay(speckle=True):
    gj = agreement_geojson(comparison(agreement_grid(speckle)),
                           simplify_m=CELL / 2.0)
    assert gj["features"]
    return gj


def test_agreement_overlay_is_not_cell_shaped():
    geoms = [to_utm(f["geometry"]) for f in agreement_overlay()["features"]]
    excess = perimeter_excess(geoms)
    assert excess < 1.08, (                            # measured 1.004
        "agreement overlay still staircased: perimeter excess %.3f" % excess)


def test_agreement_overlay_does_not_fragment_into_specks():
    """The failure the map actually showed: hundreds of cell-sized blobs."""
    for f in agreement_overlay()["features"]:
        parts = n_parts(shape(f["geometry"]))
        assert parts <= 8, "agreement class %d fragmented into %d polygons" % (
            f["properties"]["agreement"], parts)       # measured 1; was ~100


def test_agreement_overlay_keeps_every_real_feature():
    """Dropping lone cells is intended; dropping a real band is not."""
    agree = agreement_grid()
    gj = agreement_geojson(comparison(agree), simplify_m=CELL / 2.0)
    codes = {f["properties"]["agreement"]: f for f in gj["features"]}
    assert set(codes) == {AGREE_HIT, AGREE_MISS, AGREE_FALSE}
    for code, f in codes.items():
        real = connected_area_m2(agree == code)
        got = to_utm(f["geometry"]).area
        assert abs(got - real) / real < 0.04, (        # measured +1.0% worst
            "class %d: %.3f vs %.3f km2" % (code, got / 1e6, real / 1e6))


def test_agreement_overlay_matches_raster_when_unspeckled():
    """With no speckle to drop, the overlay must track the raster closely."""
    agree = agreement_grid(speckle=False)
    gj = agreement_geojson(comparison(agree), simplify_m=CELL / 2.0)
    for f in gj["features"]:
        raw = (agree == f["properties"]["agreement"]).sum() * CELL_AREA
        got = to_utm(f["geometry"]).area
        assert abs(got - raw) / raw < 0.03             # measured +0.8%


def test_agreement_classes_stay_mutually_exclusive():
    """HIT/MISS/FALSE are one partition of the raster; overlaps are a bug."""
    geoms = [shape(f["geometry"]) for f in agreement_overlay()["features"]]
    for i in range(len(geoms)):
        for j in range(i + 1, len(geoms)):
            overlap = geoms[i].intersection(geoms[j]).area
            assert overlap / min(geoms[i].area, geoms[j].area) < 0.02


def test_depth_overlay_does_not_fragment_into_specks():
    """Isolated fringe cells must not survive as cell-sized squares."""
    gj = _depth_to_geojson(diagonal_depth(speckle=True), TRANSFORM, 0.0,
                           lambda x, y: (x, y))
    for f in gj["features"]:
        parts = n_parts(shape(f["geometry"]))
        assert parts <= 12, "depth class %d fragmented into %d polygons" % (
            f["properties"]["depth_class"], parts)


def test_control_unsmoothed_depth_speckle_fragments():
    """The control for the test above: without smoothing, the specks survive."""
    raw = vectorise_raw(diagonal_depth(speckle=True) >= THRESHOLDS[0],
                        connectivity=8)
    assert n_parts(raw) > 50
