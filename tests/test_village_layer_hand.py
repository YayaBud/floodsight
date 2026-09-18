"""Every settlement in the population layer must sit near its drainage.

A settlement coordinate is validated against terrain, not against a geocoder
(INVARIANTS.md S2). Nominatim's "Nandalur" is 11.76 km from the OSM
`place=village` node and stands **85.75 m above the nearest mapped river** by
the proxy below -- a town whose railway bridge washed out cannot be. The layer
briefly carried that geocode on the geocoder's word alone; nothing pinned it.

`compute_hydro_surfaces` cannot supply real HAND in this environment (pysheds
0.5 calls `np.in1d`, removed in NumPy 2.0 -- run_pipeline.py:305 documents it),
so this uses a proxy: elevation minus the elevation of the nearest cell of the
OSM-mapped river network. It is not HAND -- it ignores flow direction -- but it
separates valley-floor settlements (measured: -1.27 to 14.19 m over all 23
features, 2026-09-13) from hillside geocodes by an order of magnitude, which is
the failure this test exists to catch.
"""
import json
from pathlib import Path

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
ndimage = pytest.importorskip("scipy.ndimage")

ROOT = Path(__file__).resolve().parents[1]
DEM = ROOT / "data" / "dem" / "annamayya_dem.tif"
LAYER = ROOT / "data" / "admin" / "annamayya_villages.geojson"

# Whole layer measures 14.19 m at its worst (Akepadu); the bad geocode measures
# 85.75 m. 30 m sits between them with room on both sides -- wide enough that a
# legitimate terrace settlement does not trip it, tight enough to catch a
# hillside.
MAX_HAND_M = 30.0


def _hand_proxy():
    import rasterio.features
    from src.data_fetcher import build_rivers

    with rasterio.open(DEM) as src:
        z = src.read(1).astype(float)
        transform, crs, nodata = src.transform, src.crs, src.nodata
    bad = ~np.isfinite(z)
    if nodata is not None:
        bad |= z == nodata
    z = np.where(bad, np.nanmax(z[~bad]), z)

    rivers = build_rivers("annamayya").to_crs(crs)
    mask = rasterio.features.rasterize(
        ((g, 1) for g in rivers.geometry), out_shape=z.shape,
        transform=transform, fill=0, dtype="uint8").astype(bool)
    assert mask.any(), "no mapped river in the domain -- the proxy has no datum"

    _, (ii, jj) = ndimage.distance_transform_edt(~mask, return_indices=True)
    return z - z[ii, jj], transform, crs


def _sample(hand, transform, crs, lon, lat):
    from pyproj import Transformer
    x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(lon, lat)
    r, c = rasterio.transform.rowcol(transform, x, y)
    if not (0 <= r < hand.shape[0] and 0 <= c < hand.shape[1]):
        return None
    return float(hand[r, c])


@pytest.fixture(scope="module")
def hand():
    if not DEM.exists():
        pytest.skip(f"{DEM} not fetched")
    return _hand_proxy()


def test_every_settlement_sits_near_its_drainage(hand):
    h, transform, crs = hand
    features = json.loads(LAYER.read_text(encoding="utf-8"))["features"]
    assert features, "population layer is empty"

    offenders = []
    for f in features:
        p = f["properties"]
        v = _sample(h, transform, crs, p["lon"], p["lat"])
        if v is None:
            offenders.append((p["village_id"], "outside the DEM"))
        elif v > MAX_HAND_M:
            offenders.append((p["village_id"], f"{v:.2f} m above nearest drainage"))
    assert not offenders, f"settlements fail the terrain check: {offenders}"


def test_the_bad_geocode_would_fail_this(hand):
    """Positive control: the check is only worth having if it rejects the error
    that got past us. Nominatim "Nandalur" = 14.3330 / 79.1961."""
    h, transform, crs = hand
    v = _sample(h, transform, crs, 79.1961, 14.3330)
    if v is None:
        pytest.skip("the bad geocode lies outside the current DEM footprint")
    assert v > MAX_HAND_M, (
        f"the geocode that was wrong measures {v:.2f} m -- the threshold no "
        "longer separates a hillside from a valley floor")


def test_nandalur_is_on_the_osm_village_node():
    """Pins the correction, so the in-channel point cannot quietly return."""
    features = json.loads(LAYER.read_text(encoding="utf-8"))["features"]
    v011 = next(f for f in features if f["properties"]["village_id"] == "V011")
    p = v011["properties"]
    assert (round(p["lon"], 4), round(p["lat"], 4)) == (79.1080, 14.2704), \
        "V011 is not on OSM node 245626858"
    assert p["pop_total"] == 5481, "population must stay on its sourced value"
