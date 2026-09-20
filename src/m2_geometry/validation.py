"""Fail-closed physical geometry checks for the hydraulic coupling boundary."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import rasterio
import rasterio.features
from scipy import ndimage
from pyproj import CRS, Transformer
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform as project

from src.m2_geometry.dem_utils import escape_head_4connected
from src.m2_geometry.seed_walk import walk_seed


class GeometryValidationError(ValueError):
    """Raised when geometry cannot support a hydraulic run.

    Carries ``.result`` -- whatever `validate_geometry` had computed by the time
    it refused. ``str(exc)`` stays exactly the message, so callers that only
    read the string are unaffected; the dict is there so a refusal does not have
    to be re-diagnosed by hand to learn WHY it fired.
    """

    def __init__(self, message: str, result: dict[str, Any] | None = None):
        super().__init__(message)
        self.result = result or {}


class ImpoundmentDoesNotHoldError(GeometryValidationError):
    """The geometry is structurally valid but the pool will not stay put.

    Distinct from its parent so that callers can tell "this manifest is
    malformed / its roles do not resolve / its seeds are not separated" apart
    from "every structural check passed and the impoundment still spills before
    the dam is loaded". The pipeline treats both as fatal -- it catches
    `GeometryValidationError` -- but the two need different fixes: the first is
    a manifest to repair, the second is a water level, a DEM resolution or a
    blockage location to correct.

    Carries ``.result`` -- everything `validate_geometry` had computed by the
    time it refused, including the derived seeds and the rasterized masks. The
    structural work is still valid, so callers that were only ever asking
    about it (seed derivation, mask rasterization) can read it without a
    bypass flag having to exist on the production entry point. The channel
    itself is inherited from `GeometryValidationError`.
    """


_REQUIRED = {"breach_point", "breach_zone", "river", "upstream_seed", "downstream_seed"}
_BARRIER = {"dam_body", "blockage"}


def _finite_geojson(obj: Any) -> bool:
    if isinstance(obj, dict):
        return all(_finite_geojson(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return all(_finite_geojson(v) for v in obj)
    if isinstance(obj, (int, float)):
        return math.isfinite(float(obj))
    return True


def validate_geometry(manifest: dict[str, Any], elevation: np.ndarray,
                      transform, crs) -> dict[str, Any]:
    """Validate canonical scenario geometry and prepare raster coupling masks."""
    if not isinstance(elevation, np.ndarray) or elevation.ndim != 2:
        raise GeometryValidationError("elevation must be a 2-D array")
    dem_crs = CRS.from_user_input(crs)
    if not dem_crs.is_projected:
        raise GeometryValidationError("DEM CRS must be projected")
    units = {str(a.unit_name).lower() for a in dem_crs.axis_info if a.unit_name}
    if units and not units <= {"metre", "meter", "metres", "meters"}:
        raise GeometryValidationError("DEM CRS must use metre units")
    if abs(transform.a * transform.e - transform.b * transform.d) <= np.finfo(float).eps:
        raise GeometryValidationError("DEM transform must be invertible with nonzero cell size")

    fc = manifest.get("geometry", manifest)
    if fc.get("type") != "FeatureCollection":
        raise GeometryValidationError("geometry must be a FeatureCollection")
    scenario_key = manifest.get("scenario_key")
    if not scenario_key or manifest.get("schema_version") != 1:
        raise GeometryValidationError("geometry manifest requires schema_version 1 and scenario_key")
    if not manifest.get("vertical_datum") or not manifest.get("dem_vertical_datum"):
        raise GeometryValidationError("vertical datum must be declared for geometry and DEM")
    # P2 (audit SS37): the elevation the barrier is emplaced at is a physical
    # property of the structure and must be sourced like every other geometry
    # field. It used to be `wse_m + 5.0` -- an arbitrary freeboard invented at
    # six call sites purely to close the seeded fill. A scenario that cannot
    # source a crest FAILS here; it does not get a default.
    crest_elev_m = manifest.get("crest_elev_m")
    if not isinstance(crest_elev_m, (int, float)) or not math.isfinite(float(crest_elev_m)):
        raise GeometryValidationError(
            "geometry manifest requires a finite sourced crest_elev_m — the "
            "barrier cannot be emplaced at an assumed elevation")
    if not manifest.get("crest_elev_source") or not manifest.get("crest_elev_classification"):
        raise GeometryValidationError(
            "crest_elev_m requires crest_elev_source and crest_elev_classification")
    crest_elev_m = float(crest_elev_m)
    if manifest["vertical_datum"] != manifest["dem_vertical_datum"]:
        raise GeometryValidationError("geometry and DEM vertical datums differ")
    features = fc.get("features", [])
    if not features:
        raise GeometryValidationError("geometry FeatureCollection is empty")
    by_role: dict[str, list[Any]] = {}
    sources: dict[str, str] = {}
    for feature in features:
        props = feature.get("properties") or {}
        role = props.get("role")
        if not role or not props.get("source") or not props.get("classification"):
            raise GeometryValidationError("every geometry feature needs role, source and classification")
        if not _finite_geojson(feature.get("geometry")):
            raise GeometryValidationError(f"non-finite geometry for role {role}")
        geom = shape(feature.get("geometry"))
        if geom.is_empty or not geom.is_valid:
            raise GeometryValidationError(f"invalid geometry for role {role}")
        by_role.setdefault(role, []).append(geom)
        sources[role] = str(props["source"])
    if not (_BARRIER & set(by_role)):
        raise GeometryValidationError("required geometry role unavailable: dam_body or blockage")
    missing = sorted(_REQUIRED - set(by_role))
    if missing:
        raise GeometryValidationError(f"required geometry roles unavailable: {', '.join(missing)}")

    # Inputs are canonical WGS84 GeoJSON. Project with axis order explicit.
    to_dem = Transformer.from_crs("EPSG:4326", dem_crs, always_xy=True).transform
    projected = {role: [project(to_dem, geom) for geom in geoms]
                 for role, geoms in by_role.items()}
    xmin, ymin, xmax, ymax = rasterio.transform.array_bounds(elevation.shape[0], elevation.shape[1], transform)
    dem_bounds = shape({"type": "Polygon", "coordinates": [[
        [xmin, ymin], [xmin, ymax], [xmax, ymax], [xmax, ymin], [xmin, ymin]]
    ]})
    for role, geoms in projected.items():
        if any(not dem_bounds.covers(geom) for geom in geoms):
            raise GeometryValidationError(f"{role} lies outside DEM coverage")
    barrier = projected.get("dam_body", projected.get("blockage"))[0]
    breach = projected["breach_zone"][0]
    river = projected["river"][0]
    breach_point = projected["breach_point"][0]
    if not barrier.intersects(breach) or not barrier.intersects(river):
        raise GeometryValidationError("breach and river must intersect barrier")
    if not river.distance(breach_point) <= max(abs(transform.a), abs(transform.e)) * 2:
        raise GeometryValidationError("breach point is not resolved on river at DEM resolution")
    # Seeds are DERIVED here, at the resolution actually in play, by walking
    # the river from the breach point until clear of the barrier buffered by
    # this DEM's cellsize -- the same algorithm
    # generate_geometry_manifest.py used to author the manifest once at full
    # resolution (src/m2_geometry/seed_walk.py::walk_seed). The manifest's
    # stored upstream_seed/downstream_seed coordinates are a fixed full-res
    # snapshot and are NOT trusted for pass/fail here: a barrier buffer that
    # scales with cellsize can swallow a fixed point at any coarser
    # resolution even though a valid clear point still exists on the river.
    if not isinstance(projected["upstream_seed"][0], Point) or not isinstance(projected["downstream_seed"][0], Point):
        raise GeometryValidationError("upstream_seed and downstream_seed must be points")
    if not isinstance(river, LineString):
        raise GeometryValidationError("river must resolve to a single LineString for seed derivation")
    pixel_m = max(abs(transform.a), abs(transform.e))
    barrier_clear = barrier.buffer(pixel_m)
    breach_xy = (breach_point.x, breach_point.y)
    # NOTE on direction sign: generate_geometry_manifest.py labels its
    # direction=+1 walk "upstream" and direction=-1 "downstream" for the
    # in-memory river LineString it just clipped from OSM. Empirically, that
    # labeling does NOT survive round-tripping the river through the written
    # WGS84 GeoJSON and back (confirmed against both real validating
    # manifests: rishiganga and phutkal) -- direction=+1 here lands within
    # ~13 m of the manifest's stored downstream_seed, and direction=-1 lands
    # within ~13 m of its stored upstream_seed, i.e. swapped relative to the
    # authoring script's own labels. Matching the manifest's established
    # upstream/downstream labeling (verified against both real manifests,
    # not assumed) requires the opposite assignment from the authoring
    # script's.
    downstream_xy = walk_seed(river, breach_xy, barrier_clear, direction=+1)
    upstream_xy = walk_seed(river, breach_xy, barrier_clear, direction=-1)
    seeds = [Point(upstream_xy), Point(downstream_xy)]
    if barrier_clear.contains(seeds[0]) or barrier_clear.contains(seeds[1]):
        # walk_seed ran off the end of the river (its "terminal vertex"
        # fallback) without ever clearing the barrier at this resolution --
        # that fallback point is not guaranteed clear, so it must fail here
        # rather than silently pass a seed still inside the barrier.
        raise GeometryValidationError("river seeds must lie outside barrier")

    expected_types = {
        "breach_point": {"Point"}, "upstream_seed": {"Point"},
        "downstream_seed": {"Point"}, "river": {"LineString", "MultiLineString"},
        "dam_body": {"Polygon", "MultiPolygon"}, "blockage": {"Polygon", "MultiPolygon"},
        "breach_zone": {"Polygon", "MultiPolygon"},
    }
    for role, allowed in expected_types.items():
        if role in projected and any(g.geom_type not in allowed for g in projected[role]):
            raise GeometryValidationError(f"invalid geometry type for role {role}")

    barrier_mask = rasterio.features.rasterize(
        [(barrier, 1)], out_shape=elevation.shape, transform=transform,
        fill=0, dtype="uint8").astype(bool)
    breach_mask = rasterio.features.rasterize(
        [(breach, 1)], out_shape=elevation.shape, transform=transform,
        fill=0, dtype="uint8").astype(bool)
    if not barrier_mask.any() or not breach_mask.any():
        raise GeometryValidationError("geometry unresolved at DEM resolution")
    level = manifest.get("water_level_m")
    if not isinstance(level, (int, float)) or not np.isfinite(level):
        raise GeometryValidationError("finite water_level_m required for connectivity validation")
    valid = np.isfinite(elevation) & (elevation <= float(level))
    blocked = valid & ~barrier_mask
    labels, n_components = ndimage.label(blocked, structure=np.ones((3, 3), dtype=bool))
    upstream_rc = tuple(int(v) for v in rasterio.transform.rowcol(transform, *upstream_xy))
    downstream_rc = tuple(int(v) for v in rasterio.transform.rowcol(transform, *downstream_xy))
    ur, uc = upstream_rc
    dr, dc = downstream_rc
    if not (0 <= ur < elevation.shape[0] and 0 <= uc < elevation.shape[1] and
            0 <= dr < elevation.shape[0] and 0 <= dc < elevation.shape[1]):
        raise GeometryValidationError("seed outside DEM grid")
    upstream_label = int(labels[ur, uc])
    downstream_label = int(labels[dr, dc])
    if upstream_label == 0 or downstream_label == 0 or upstream_label == downstream_label:
        # The bare message cost a full session to diagnose once, because the
        # three distinct causes it covers -- a seed above water, and a barrier
        # that fails to cut the wet region -- are indistinguishable from the
        # string. The numbers that DO distinguish them are already computed
        # here, so they travel with the refusal rather than being re-derived.
        #
        # The discriminator is `barrier_end_elev_m` against `water_level_m`: a
        # barrier whose ends stand above the level has real abutments and a
        # genuine terrain problem, while one with an end below the level is not
        # spanning its valley at all. Extending such a barrier until the labels
        # separate is REJECTED (scripts/generate_geometry_manifest.py:64-77, and
        # findings_results.md 2026-09-19 measured the end elevations that make
        # it wrong) -- it splits the components while water still spills around.
        ends: list[float] = []
        try:
            hull = barrier if barrier.geom_type == "Polygon" else barrier.convex_hull
            pts = np.asarray(hull.exterior.coords)[:, :2]
            centre = pts.mean(axis=0)
            axis = np.linalg.svd(pts - centre, full_matrices=False)[2][0]
            offs = (pts - centre) @ axis
            for end in (centre + axis * offs.max(), centre + axis * offs.min()):
                er, ec = (int(v) for v in rasterio.transform.rowcol(transform, *end))
                if 0 <= er < elevation.shape[0] and 0 <= ec < elevation.shape[1]:
                    ends.append(round(float(elevation[er, ec]), 2))
            span_m = round(float(offs.max() - offs.min()), 1)
        except Exception:                                     # noqa: BLE001
            span_m = None
        same = upstream_label != 0 and upstream_label == downstream_label
        raise GeometryValidationError(
            "upstream/downstream seeds lack separated connected components",
            {"cause": ("barrier does not cut the wet region" if same
                       else "a seed lies above water_level_m"),
             "water_level_m": float(level),
             "wet_cells_below_level": int(valid.sum()),
             "components_below_level": int(n_components),
             "upstream_label": upstream_label,
             "downstream_label": downstream_label,
             "shared_component_cells": (int((labels == upstream_label).sum())
                                        if same else None),
             "upstream_seed_elev_m": round(float(elevation[ur, uc]), 2),
             "downstream_seed_elev_m": round(float(elevation[dr, dc]), 2),
             "barrier_cells": int(barrier_mask.sum()),
             "barrier_span_m": span_m,
             "barrier_end_elev_m": ends,
             "barrier_ends_above_level": (bool(ends) and min(ends) >= float(level))})
    upstream_basin = labels == upstream_label
    if (upstream_basin[0, :].any() or upstream_basin[-1, :].any() or
            upstream_basin[:, 0].any() or upstream_basin[:, -1].any()):
        raise GeometryValidationError("upstream pool reaches DEM edge")
    # ── P2 barrier-continuity / retention gate (audit SS37) ───────────────────
    # Everything above proves the barrier INTERSECTS the breach zone and the
    # river, and that the two seeds sit in separated components. None of it
    # proves the impoundment HOLDS at the resolution actually being run, and
    # two different things can break that:
    #
    #  1. The barrier is too thin. `swe_2d._rhs` treats the bed as continuous
    #     piecewise-linear -- the bed at a face is the AVERAGE of the two cells
    #     it separates -- which is what makes the scheme well-balanced and also
    #     means a barrier ONE CELL thick does not exist to the solver: both its
    #     faces average it against a lower neighbour. Measured on a synthetic
    #     28 m grid with a pool held 5 m below a 160 m crest, a one-cell wall
    #     passes 46.7 % of the pool in 300 s and a two-cell wall passes
    #     0.000 m^3.
    #  2. The bowl's own rim is lower than the level being filled, so the water
    #     leaves over the valley wall without ever reaching the structure.
    #     Measured on rishiganga at 28 m: the lowest rim cell off the barrier
    #     sits at 2170.73 m while the pool is filled to the 2175.68 m crest, so
    #     the impoundment spills 4.95 m before the dam is loaded at all.
    #
    # Both are answered by one question, asked on the terrain WITH the barrier
    # emplaced and with the face-averaged bed the solver integrates: at what
    # level does water first escape the upstream basin? If that is below the
    # water level, the impoundment does not hold and the run would be a pool
    # spreading downhill rather than a dam failing.
    dem_emplaced = np.where(barrier_mask & (elevation < crest_elev_m),
                            crest_elev_m, elevation)
    outside_basin = ~upstream_basin & ~barrier_mask
    spill_level = escape_head_4connected(
        dem_emplaced, ur, uc, interface_bed=True, target_mask=outside_basin)
    # NOT a raise. Retention is a physics verdict, not a malformed manifest, so
    # it is reported as gate G4 in run_pipeline's validity block alongside
    # G1-G3 -- the same pattern the flow-regime gate uses: the run proceeds and
    # produces measurable output, and nothing certifies it as valid. Making it
    # fatal here would leave the project with zero runnable scenarios and no
    # instrument to measure the release physics with.
    opened = valid.copy()
    source_rows, source_cols = np.where(breach_mask & opened)
    if source_rows.size == 0:
        raise GeometryValidationError("breach opening has no valid downstream source cell")
    opened_labels, _ = ndimage.label(opened, structure=np.ones((3, 3), dtype=bool))
    if not any(opened_labels[rr, cc] == downstream_label for rr, cc in zip(source_rows, source_cols)):
        raise GeometryValidationError("breach opening is not connected to downstream seed")
    return {
        "schema_version": 1, "scenario_key": scenario_key,
        "valid": True, "crs": dem_crs.to_string(),
        "roles": sorted(by_role), "sources": sources,
        "barrier_mask": barrier_mask, "breach_mask": breach_mask,
        "crest_elev_m": crest_elev_m,
        "spill_level_m": float(spill_level),
        "crest_elev_source": str(manifest["crest_elev_source"]),
        "crest_elev_classification": str(manifest["crest_elev_classification"]),
        "upstream_basin_mask": upstream_basin,
        "downstream_source_weights": breach_mask.astype(float) / max(1, int(breach_mask.sum())),
        "upstream_seed_rc": upstream_rc, "downstream_seed_rc": downstream_rc,
        "upstream_seed_xy": upstream_xy, "downstream_seed_xy": downstream_xy,
        "projected_geometry": {role: [geom.__geo_interface__ for geom in geoms]
                               for role, geoms in projected.items()},
    }
