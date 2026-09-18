"""
M10 — Observed flood outcomes
=============================
Loads real, independently-observed flood data and scores a FloodSight run
against it. Everything here is an *observation*: none of it was produced by
this project, and none of it may ever be relabelled as a model output.

What is wired
-------------
``derna``  Copernicus EMS activation **EMSR696** — Derna, Libya, 11 Sep 2023.
           Storm Daniel destroyed the Abu Mansour and Al-Bilad dams upstream of
           the city. Professionally delineated flood extent, road damage grades
           and building damage points. A real double dam-break with a mapped
           outcome: the closest available analogue to what this project
           simulates.

See ``data/validation/README.md`` for provenance, licences and the datasets
that were downloaded but are *not* wired here (GFD rasters at 250 m,
Sen1Floods11 SAR labels, the Malpasset meshes).

The domain rule
---------------
Copernicus maps an Area of Interest. Outside it there is no observation — not
an observation of dryness. Scoring is therefore clipped to the AOI polygons,
which are read from the activation metadata (``ems696.json``) rather than
guessed from the bounding box of the flood polygons, because the flood does not
fill its own AOI and using its hull would silently delete every false alarm.
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from scipy.ndimage import gaussian_filter, zoom
import geopandas as gpd
import rasterio
import rasterio.features
from shapely import wkt
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform, unary_union
from pyproj import Transformer
from affine import Affine

from ..provenance import Provenance
from .metrics import (ExtentSkill, confusion, agreement_map,
                      AGREE_HIT, AGREE_MISS, AGREE_FALSE, AGREE_LABELS)

logger = logging.getLogger(__name__)

VALIDATION_DIR = Path(__file__).resolve().parents[2] / "data" / "validation"


@dataclass(frozen=True)
class ObservedSource:
    """Where an observed outcome comes from, and what it covers."""
    key: str
    event: str
    source: str
    licence: str
    activation_json: Optional[str]     # relative to VALIDATION_DIR
    extent_glob: str
    roads_glob: Optional[str]
    buildings_glob: Optional[str]
    #: Observations are not ours and are not live. PRECOMPUTED is the honest
    #: slot in the existing lattice; the UI shows `source` next to it so the
    #: badge never implies we computed the truth.
    provenance: Provenance = Provenance.PRECOMPUTED


SOURCES: dict[str, ObservedSource] = {
    "derna": ObservedSource(
        key="derna",
        event=("Derna, Libya — Abu Mansour + Al-Bilad dam collapse, "
               "11 September 2023"),
        source="Copernicus EMS Rapid Mapping, activation EMSR696",
        licence="Copernicus EMS — free and open, attribution required",
        activation_json="ems696.json",
        extent_glob="ems/EMSR696/*observedEventA*.json",
        roads_glob="ems/EMSR696/*transportationL*.json",
        buildings_glob="ems/EMSR696/*builtUpP*.json",
    ),
    # ivanovo, malpasset and annamayya were registered here against
    # hand-authored "observed" extents (a 5-vertex rectangle for ivanovo,
    # an SAR-branded polygon for annamayya). Those files were fabricated,
    # are deleted, and the sources are unregistered rather than left
    # pointing at nothing. Re-add a key only with a real, citable product.
}


def available(scenario_key: str) -> bool:
    """True only when source metadata and bundled observed artifacts exist."""
    src = SOURCES.get(scenario_key)
    if src is None:
        return False
    try:
        _validate_source_identity(scenario_key)
        return bool(glob.glob(str(VALIDATION_DIR / src.extent_glob)))
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _validate_source_identity(scenario_key: str) -> None:
    """Validate bundled artifact identity before any loader certifies it."""
    src = SOURCES.get(scenario_key)
    if src is None:
        raise ValueError(f"unknown observed source: {scenario_key}")
    if scenario_key != "derna":
        raise ValueError(f"{scenario_key}: observed artifact identity is not registered")
    meta = VALIDATION_DIR / "ems696.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    results = data.get("results") or []
    if not results or results[0].get("code") != "EMSR696":
        raise ValueError("Derna artifact identity is not EMSR696")
    for path in glob.glob(str(VALIDATION_DIR / src.extent_glob)):
        p = Path(path).resolve()
        if VALIDATION_DIR.resolve() not in p.parents:
            raise ValueError("observed artifact escapes validation directory")
        if not p.is_file() or not hashlib.sha256(p.read_bytes()).hexdigest():
            raise ValueError(f"observed artifact cannot be hashed: {path}")


def describe(scenario_key: str) -> Optional[dict]:
    """Metadata for the dashboard. None when nothing is wired."""
    src = SOURCES.get(scenario_key)
    if src is None or not available(scenario_key):
        return None
    return {
        "event": src.event,
        "source": src.source,
        "licence": src.licence,
        "provenance": str(src.provenance),
        "classification": "OBSERVED",
        "artifact_files": sorted(Path(p).relative_to(VALIDATION_DIR).as_posix()
                                  for p in glob.glob(str(VALIDATION_DIR / src.extent_glob))),
    }


def _read_many(pattern: str) -> gpd.GeoDataFrame:
    """Concatenate every file matching a glob into one WGS84 GeoDataFrame."""
    paths = sorted(glob.glob(str(VALIDATION_DIR / pattern)))
    if not paths:
        raise FileNotFoundError(f"no observed data matching {pattern}")
    frames = []
    for p in paths:
        g = gpd.read_file(p)
        if len(g):
            g["aoi"] = Path(p).name.split("_")[1]      # EMSR696_AOI01_... -> AOI01
            frames.append(g.to_crs("EPSG:4326"))
    if not frames:
        raise ValueError(f"every file matching {pattern} was empty")
    out = gpd.GeoDataFrame(
        __import__("pandas").concat(frames, ignore_index=True), crs="EPSG:4326")
    return out


def load_observed_extent(scenario_key: str) -> gpd.GeoDataFrame:
    """Observed flood polygons, WGS84."""
    if not available(scenario_key):
        raise FileNotFoundError(f"{scenario_key}: verified observed extent unavailable")
    return _read_many(SOURCES[scenario_key].extent_glob)


def load_observed_roads(scenario_key: str) -> gpd.GeoDataFrame:
    """Observed road links with damage grades, WGS84."""
    g = SOURCES[scenario_key].roads_glob
    if not g:
        raise FileNotFoundError(f"no observed roads wired for {scenario_key}")
    if not available(scenario_key):
        raise FileNotFoundError(f"{scenario_key}: verified observed roads unavailable")
    return _read_many(g)


def load_observed_domain(scenario_key: str) -> gpd.GeoDataFrame:
    """
    The mapped Areas of Interest — the only cells where an observation exists.

    Read from activation metadata. Missing metadata means NOT_AVAILABLE.
    """
    src = SOURCES[scenario_key]
    if not available(scenario_key):
        raise FileNotFoundError(f"{scenario_key}: verified observed AOI unavailable")
    meta = VALIDATION_DIR / (src.activation_json or "")
    if src.activation_json and meta.exists():
        with open(meta) as f:
            data = json.load(f)
        geoms, names = [], []
        for result in data.get("results", []):
            for aoi in result.get("aois", []):
                ext = aoi.get("extent")
                if not ext:
                    continue
                try:
                    geoms.append(wkt.loads(ext))
                    names.append(f"AOI{int(aoi.get('number', 0)):02d}")
                except Exception as exc:            # noqa: BLE001 - report, skip
                    logger.warning("AOI %s: unparseable extent (%s)",
                                   aoi.get("name"), exc)
        if geoms:
            return gpd.GeoDataFrame({"aoi": names}, geometry=geoms,
                                    crs="EPSG:4326")

    raise FileNotFoundError(
        f"{scenario_key}: observed AOI metadata unavailable; convex-hull scoring disabled")


def _rasterize(gdf: gpd.GeoDataFrame, shape_hw, transform, crs) -> np.ndarray:
    """Burn polygons onto a raster grid as a boolean mask."""
    if gdf.crs is None:
        raise ValueError("observed geometry has no CRS")
    g = gdf.to_crs(crs)
    shapes = [(geom, 1) for geom in g.geometry if geom is not None
              and not geom.is_empty]
    if not shapes:
        return np.zeros(shape_hw, dtype=bool)
    burned = rasterio.features.rasterize(
        shapes, out_shape=shape_hw, transform=transform,
        fill=0, dtype="uint8", all_touched=True,
    )
    return burned.astype(bool)


@dataclass
class ExtentComparison:
    """Result of scoring one simulated extent against one observed extent."""
    skill: ExtentSkill
    agreement: np.ndarray
    transform: object
    crs: object
    cell_area_m2: float
    threshold_m: float
    source: ObservedSource
    overlapped: bool

    def areas_km2(self) -> dict:
        a = self.cell_area_m2 / 1e6
        return {
            "simulated_km2": round(self.skill.sim_area_cells * a, 3),
            "observed_km2":  round(self.skill.obs_area_cells * a, 3),
            "hit_km2":       round(self.skill.tp * a, 3),
            "miss_km2":      round(self.skill.fn * a, 3),
            "false_alarm_km2": round(self.skill.fp * a, 3),
            "scored_km2":    round(self.skill.n_scored * a, 3),
        }


def compare_extent(
    max_depth_raster: str | Path,
    scenario_key: str,
    threshold_m: float = 0.30,
) -> ExtentComparison:
    """
    Score a simulated maximum-depth raster against the observed flood extent.

    The simulation grid is authoritative: the observation is rasterised *onto*
    it, never the reverse. Resampling a hand-delineated polygon up to the
    simulation's cell size would destroy the thing being validated.
    """
    src = SOURCES[scenario_key]
    with rasterio.open(max_depth_raster) as ds:
        depth = ds.read(1).astype(float)
        transform, crs = ds.transform, ds.crs
        cell_area = abs(transform.a * transform.e)

    obs_gdf = load_observed_extent(scenario_key)
    dom_gdf = load_observed_domain(scenario_key)

    obs = _rasterize(obs_gdf, depth.shape, transform, crs)
    domain = _rasterize(dom_gdf, depth.shape, transform, crs)
    valid_depth = np.isfinite(depth)
    domain &= valid_depth
    sim = np.where(valid_depth, depth >= threshold_m, False)

    overlapped = bool(domain.any())
    if not overlapped:
        logger.warning(
            "%s: the simulation domain does not intersect any observed AOI. "
            "No score is produced — an empty overlap is not a CSI of zero.",
            scenario_key)

    skill = confusion(sim, obs, domain)
    agree = agreement_map(sim, obs, domain)

    logger.info("M10: %s vs %s — %s", scenario_key, src.source, skill.summary())
    return ExtentComparison(
        skill=skill, agreement=agree, transform=transform, crs=crs,
        cell_area_m2=cell_area, threshold_m=threshold_m, source=src,
        overlapped=overlapped,
    )


def agreement_geojson(cmp: ExtentComparison, simplify_m: float = 0.0) -> dict:
    """
    Vectorise the agreement raster for the map overlay.

    Uses 8-connectivity. The 4-connected default splits a diagonal wet band
    into a staircase of disconnected squares, which is a rendering artefact
    rather than a property of the flood.
    """
    to_wgs84 = Transformer.from_crs(cmp.crs, "EPSG:4326", always_xy=True).transform
    features = []
    display_scale = 4
    display_transform = cmp.transform * Affine.scale(
        1 / display_scale, 1 / display_scale)
    for code in (AGREE_HIT, AGREE_MISS, AGREE_FALSE):
        mask = cmp.agreement == code
        if not mask.any():
            continue
        # order=1, not 0: nearest-neighbour upsampling replicates each cell
        # into an identical block, so the blur had no sub-cell detail to work
        # with and the squares survived verbatim. sigma scales with
        # display_scale for the same reason as in the depth path. The level is
        # 0.5, not 0.45, so HIT/MISS/FALSE stay mutually exclusive instead of
        # all three dilating into each other.
        display_mask = gaussian_filter(
            zoom(mask.astype(np.float32), display_scale, order=1),
            sigma=display_scale * 0.9,
        ) >= 0.5
        polys = [shape(geom) for geom, val in rasterio.features.shapes(
            display_mask.astype(np.uint8), mask=display_mask,
            transform=display_transform,
            connectivity=8) if val == 1]
        if not polys:
            continue
        merged = unary_union(polys)
        if simplify_m > 0:
            # Straighten the boundary without changing the cell-level
            # agreement raster used for the reported scores. The buffer pair
            # this replaces was a closing, which does not touch convex corners.
            merged = merged.simplify(simplify_m, preserve_topology=True)
        merged = shp_transform(to_wgs84, merged)
        if merged.is_empty:
            continue
        features.append({
            "type": "Feature",
            "properties": {"agreement": int(code),
                           "label": AGREE_LABELS[code]},
            "geometry": mapping(merged),
        })
    return {"type": "FeatureCollection", "features": features}


def _round_coords(obj, dp: int = 6):
    """
    Round every coordinate in a GeoJSON structure in place.

    6 dp is ~0.11 m: finer than a 30 m DEM cell can justify, and it roughly
    halves the payload the browser has to download, parse and tile. The
    Copernicus delineation for Derna is 1,843 polygons and 3 MB at full
    float64 precision, none of which is real accuracy at this scale.
    """
    if isinstance(obj, list):
        if obj and isinstance(obj[0], (int, float)):
            return [round(float(v), dp) for v in obj]
        return [_round_coords(v, dp) for v in obj]
    if isinstance(obj, dict):
        return {k: (_round_coords(v, dp) if k in ("coordinates", "geometry",
                                                  "features", "geometries")
                    else v)
                for k, v in obj.items()}
    return obj


def observed_extent_geojson(scenario_key: str) -> dict:
    """The observed flood polygons, as GeoJSON, for the map."""
    g = load_observed_extent(scenario_key)
    keep = [c for c in ("notation", "obj_desc", "event_type", "aoi", "area")
            if c in g.columns]
    return _round_coords(json.loads(g[keep + ["geometry"]].to_json()))


def demo() -> None:
    """Self-check that needs no downloaded data. Uses a synthetic grid."""
    from rasterio.transform import from_origin
    from shapely.geometry import box

    tr = from_origin(0.0, 100.0, 10.0, 10.0)      # 10 m cells, 10x10 grid
    shape_hw = (10, 10)
    # Observed flood: a 30x30 m box; domain: the left half of the grid.
    obs = gpd.GeoDataFrame(geometry=[box(0, 70, 30, 100)], crs="EPSG:32633")
    dom = gpd.GeoDataFrame(geometry=[box(0, 0, 50, 100)], crs="EPSG:32633")

    o = _rasterize(obs, shape_hw, tr, "EPSG:32633")
    d = _rasterize(dom, shape_hw, tr, "EPSG:32633")
    assert o.sum() == 9, o.sum()          # 3x3 cells
    assert d.sum() == 50, d.sum()         # 5 cols x 10 rows

    # A perfect simulation over the domain scores CSI 1.
    r = confusion(o, o, d)
    assert r.csi == 1.0

    # Something wet outside the domain must not be counted at all.
    sim = o.copy(); sim[0, 9] = True
    r2 = confusion(sim, o, d)
    assert r2.fp == 0, "a wet cell outside the observed AOI was scored"

    print("m10_validation.observed: all checks passed")


if __name__ == "__main__":
    demo()
