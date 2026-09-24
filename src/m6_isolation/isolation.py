"""
M6 — Time-Varying Road Isolation Analysis  ★ THE DIFFERENTIATOR ★
===================================================================
Answers the question no operational Indian system answers:

    "Which villages lose their last road out — and when?"

This is the isolation-time analysis. It is operationally useful because the
evacuation window is the gap between losing road access and the water arriving.
A village with a 34-minute window and 1,240 people requires a different
response from one with a 3-hour window and 40 people.

Method
------
1. Load the OSM road network for the AOI (cached GraphML), project it once to
   the depth raster's CRS, and pre-compute every edge midpoint.
2. For each simulation timestep t:
   a. Sample the depth raster at every edge midpoint.
   b. Cut edges where depth >= the vehicle threshold.
   c. Label connected components of the surviving graph.
   d. A village is ISOLATED at t if its nearest road node shares no component
      with any node in the safe set.
3. Isolation time = first t at which the village is isolated.
4. Evacuation window = water_arrival_time - isolation_time.

The safe set
------------
Safe nodes are recomputed at every timestep from that timestep's own depth
raster — a node is safe at time t if it is dry (< threshold) at time t, not if
it happens to stay dry for the rest of the run. Using the max-depth raster to
build one fixed safe set for the whole simulation would leak knowledge of the
future into the present: a node that only floods at T+200 min would already be
excluded from the safe set at T+0, so any village whose sole route runs through
it would be reported isolated from the very first frame. The safe set is
therefore per-timestep and self-consistent with only what has happened by t.

Before any flooding is applied, a one-off baseline pass over the full, uncut
road graph checks whether each village's snapped node has road connectivity at
all when dry. A node that is isolated even in the unflooded graph (a stub with
no through-path) is a pre-existing OSM gap, not a flood effect, and is flagged
``OSM_GAP`` rather than being attributed a flood-caused isolation time.

Depth thresholds (cited, and labelled on screen)
------------------------------------------------
- Cars   : 0.30 m — most passenger cars stall above this
- Trucks : 0.50 m — light commercial vehicles
- Bridges: OSM tags rarely carry deck elevation, so bridge edges use the same
  threshold and are counted separately so the assumption stays visible.

No operational Indian system performs time-varying road-graph isolation. RBSD
shows Population at Risk; C-FLOOD shows inundation extent. Neither cuts the
graph.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd
import geopandas as gpd
import networkx as nx
import rasterio
import rasterio.features

from ..rasterutils import sample_raster

logger = logging.getLogger(__name__)

try:
    import osmnx as ox
    _OSMNX = True
except ImportError:
    _OSMNX = False
    logger.warning("OSMnx not installed. Run `pip install osmnx`")

# Depth thresholds [m]
THRESH_CAR   = 0.30
THRESH_TRUCK = 0.50


def _is_projected(crs) -> bool:
    """True when a CRS measures in metres, so distances and centroids are valid."""
    try:
        return bool(crs.is_projected)
    except AttributeError:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Graph loading
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# Geometry helpers — computed once, reused for every timestep
# ──────────────────────────────────────────────────────────────────────────────

def edge_midpoints(G_proj: nx.MultiDiGraph) -> tuple[list[tuple], np.ndarray, np.ndarray, np.ndarray]:
    """
    Pre-compute the midpoint of every edge in a projected graph.

    Returns ``(edge_keys, xs, ys, is_bridge)``. Doing this once and sampling the
    raster with array indexing is the difference between an analysis that runs
    in seconds and one that reads the whole raster once per edge per timestep.
    """
    keys: list[tuple] = []
    xs: list[float] = []
    ys: list[float] = []
    bridges: list[bool] = []

    for u, v, k, data in G_proj.edges(keys=True, data=True):
        geom = data.get("geometry")
        if geom is not None:
            mid = geom.interpolate(0.5, normalized=True)
            x, y = mid.x, mid.y
        else:
            x = (G_proj.nodes[u]["x"] + G_proj.nodes[v]["x"]) / 2.0
            y = (G_proj.nodes[u]["y"] + G_proj.nodes[v]["y"]) / 2.0
        keys.append((u, v, k))
        xs.append(x)
        ys.append(y)
        bridges.append(data.get("bridge") in ("yes", "viaduct", "aqueduct"))

    return keys, np.asarray(xs), np.asarray(ys), np.asarray(bridges, dtype=bool)


def cut_flooded_edges(
    G_proj: nx.MultiDiGraph,
    depth_arr: np.ndarray,
    transform,
    edge_keys: Sequence[tuple],
    xs: np.ndarray,
    ys: np.ndarray,
    threshold_m: float = THRESH_CAR,
    is_bridge: Optional[np.ndarray] = None,
    bridge_threshold_m: float = 3.0,
) -> nx.MultiDiGraph:
    """
    Return a copy of ``G_proj`` with edges under >= threshold of water removed.

    For bridges (if ``is_bridge`` is supplied), an edge is only cut if depth exceeds
    ``bridge_threshold_m`` (default 3.0m) to reflect deck elevation clearance above
    the riverbed. Surface roads use ``threshold_m``.

    Expects the pre-projected graph and pre-computed midpoints from
    :func:`edge_midpoints`, so the raster is read once by the caller.
    """
    depths = sample_raster(depth_arr, transform, xs, ys)
    if is_bridge is not None:
        eff_thresh = np.where(is_bridge, bridge_threshold_m, threshold_m)
        flooded = depths >= eff_thresh
    else:
        flooded = depths >= threshold_m

    G_cut = G_proj.copy()
    G_cut.remove_edges_from([edge_keys[i] for i in np.nonzero(flooded)[0]])
    logger.debug("Cut %d / %d edges", int(flooded.sum()), len(edge_keys))
    return G_cut


# ──────────────────────────────────────────────────────────────────────────────
# Isolation time computation
# ──────────────────────────────────────────────────────────────────────────────

def compute_isolation_times(
    G: nx.MultiDiGraph,
    raster_stack: Sequence[tuple[float, str | Path]],
    village_gdf: gpd.GeoDataFrame,
    max_depth_raster: str | Path,
    threshold_m: float = THRESH_CAR,
    bridge_threshold_m: float = 3.0,
) -> gpd.GeoDataFrame:
    """
    Compute per-village isolation and evacuation window across all timesteps.

    Parameters
    ----------
    G                : Full (uncut) OSMnx road graph, WGS84.
    raster_stack     : ``[(t_seconds, depth_raster_path), ...]`` in time order.
    village_gdf      : Village polygons; centroids are snapped to road nodes.
    max_depth_raster : Maximum-depth GeoTIFF for the run. Kept as a parameter
                       for callers/back-compat; no longer used to build a
                       single fixed safe set (see module docstring — the safe
                       set is now computed fresh per timestep).
    threshold_m      : Depth at which a road is treated as impassable.

    Returns
    -------
    ``village_gdf`` plus:
        ``isolation_time_s`` / ``_min``      first time the village is cut off
                                             by flooding (NaN = never isolated
                                             in the window, or not attributable
                                             to flooding — see
                                             ``isolation_status``)
        ``water_arrival_time_s`` / ``_min``  first time water reaches the centroid
        ``evacuation_window_s`` / ``_min``   arrival - isolation; how long people
                                             have between losing the road and the
                                             water arriving. Negative means the
                                             water arrives before the road is cut.
        ``road_nodes_found``                 False where the village has no road
                                             in the graph — metrics are NaN and
                                             must render as NO ROAD DATA.
        ``baseline_connected``               True if the village's road node has
                                             road connectivity in the unflooded
                                             graph (component size > 1). False
                                             means any apparent isolation is a
                                             pre-existing OSM gap, not a flood
                                             effect.
        ``isolation_status``                 One of ``"FLOOD_ISOLATED"`` (cut
                                             off by flooding), ``"NO_ROAD_DATA"``
                                             (no road node found),
                                             ``"OSM_GAP"`` (baseline_connected
                                             is False — pre-existing graph gap,
                                             isolation_time_s stays NaN), or
                                             ``"NOT_ISOLATED"`` (never isolated
                                             in the window).
        ``n_road_exits``                    Degree of the village's snapped
                                             node in the full, unflooded,
                                             undirected road graph — how many
                                             distinct road edges leave it when
                                             nothing is flooded. 0 where
                                             ``road_nodes_found`` is False.
    """
    if not _OSMNX:
        raise RuntimeError("OSMnx not installed. Run: pip install osmnx")
    if not raster_stack:
        raise ValueError("raster_stack is empty — no timesteps to analyse")

    with rasterio.open(raster_stack[0][1]) as src0:
        flood_crs = src0.crs
        transform = src0.transform

    # Project the graph and pre-compute midpoints ONCE, not per timestep.
    G_proj = ox.project_graph(G, to_crs=flood_crs)
    edge_keys, exs, eys, is_bridge = edge_midpoints(G_proj)
    logger.info("Road graph: %d nodes, %d edges (%d bridges) projected to %s",
                G_proj.number_of_nodes(), len(edge_keys), int(is_bridge.sum()),
                flood_crs)

    # ── Node positions, reused every timestep to build the per-timestep safe set ──
    node_ids = list(G_proj.nodes)
    nxs = np.array([G_proj.nodes[n]["x"] for n in node_ids])
    nys = np.array([G_proj.nodes[n]["y"] for n in node_ids])

    # ── Snap villages to the nearest road node ───────────────────────────────
    # Node ids survive projection, so the nearest-node search runs on a metric
    # graph while raster sampling stays in the raster's CRS. Searching an
    # unprojected graph would need scikit-learn and would measure degrees.
    G_metric = G_proj if _is_projected(flood_crs) else ox.project_graph(G)
    metric_crs = G_metric.graph["crs"]

    vg_metric = village_gdf.to_crs(metric_crs)
    cent_metric = vg_metric.geometry.centroid
    village_nodes: list[object | None] = list(ox.nearest_nodes(
        G_metric, cent_metric.x.to_numpy(), cent_metric.y.to_numpy()))
    road_found = np.array([n is not None for n in village_nodes], dtype=bool)

    # ── Baseline (unflooded) connectivity — separates OSM gaps from flood
    # isolation. A village node stuck in a tiny/singleton component even with
    # zero flooding applied is a pre-existing road-network gap, not something
    # the flood caused; it must never be reported as flood-isolated at t=0.
    G_undirected = G_proj.to_undirected()
    baseline_comp_size: dict[object, int] = {}
    for comp in nx.connected_components(G_undirected):
        size = len(comp)
        for node in comp:
            baseline_comp_size[node] = size
    baseline_connected = np.array([
        road_found[vi] and baseline_comp_size.get(vnode, 0) > 1
        for vi, vnode in enumerate(village_nodes)
    ], dtype=bool)

    # Per-village egress-route count: degree of the snapped node in the full,
    # unflooded, undirected graph — how many distinct road edges leave it when
    # nothing is flooded. This is the real per-village exit count M7's ranking
    # previously faked as a constant 1.
    n_road_exits = np.array([
        int(G_undirected.degree(vnode)) if road_found[vi] else 0
        for vi, vnode in enumerate(village_nodes)
    ], dtype=int)

    # Water arrival is sampled over the whole village FOOTPRINT, not just its
    # centroid. M5 declares a village inundated if any part of its polygon is
    # wet; sampling only the centre point here meant a flood could clip the
    # edge of a settlement and produce `inundated: true` alongside
    # `water_arrival_min: null` — two modules disagreeing about the same event.
    # Both now ask the same question of the same raster.
    vg_flood = village_gdf.to_crs(flood_crs)
    cent_flood = vg_flood.to_crs(metric_crs).geometry.centroid.to_crs(flood_crs)
    cx = cent_flood.x.to_numpy()
    cy = cent_flood.y.to_numpy()
    n_villages = len(village_gdf)

    # Rasterise each village once; per timestep this is then a cheap lookup
    # rather than a re-rasterisation.
    with rasterio.open(raster_stack[0][1]) as _src:
        _shape, _tr = _src.shape, _src.transform
    village_cells: list[tuple[np.ndarray, np.ndarray]] = []
    for geom in vg_flood.geometry:
        mask = rasterio.features.geometry_mask(
            [geom.__geo_interface__], out_shape=_shape, transform=_tr,
            invert=True, all_touched=True,
        )
        rows, cols = np.nonzero(mask)
        if rows.size == 0:
            # Polygon smaller than a pixel: fall back to its centre cell.
            r, c = rasterio.transform.rowcol(_tr, geom.centroid.x, geom.centroid.y)
            r = int(np.clip(r, 0, _shape[0] - 1))
            c = int(np.clip(c, 0, _shape[1] - 1))
            rows, cols = np.array([r]), np.array([c])
        village_cells.append((rows, cols))

    isolation_t = np.full(n_villages, np.nan)
    water_arr_t = np.full(n_villages, np.nan)

    logger.info("Isolation analysis: %d villages, %d timesteps",
                n_villages, len(raster_stack))

    for t_s, raster_path in raster_stack:
        with rasterio.open(raster_path) as src:
            depth_arr = src.read(1).astype(float)
            tr = src.transform

        # Water arrival anywhere within the village footprint.
        for vi, (rows, cols) in enumerate(village_cells):
            if not np.isnan(water_arr_t[vi]):
                continue
            if np.nanmax(depth_arr[rows, cols], initial=0.0) >= threshold_m:
                water_arr_t[vi] = t_s

        # Safe set for THIS timestep only — a node is safe at t if it is dry
        # (< threshold) right now, not if it happens to stay dry for the rest
        # of the run. Recomputing this every iteration (rather than once from
        # the max-depth raster) is what keeps a future flood event from
        # leaking into an earlier timestep's isolation verdict.
        node_depth_now = sample_raster(depth_arr, tr, nxs, nys)
        safe_nodes_t = {n for n, d in zip(node_ids, node_depth_now) if d < threshold_m}

        # Cut the graph and label components once for this timestep
        G_cut = cut_flooded_edges(G_proj, depth_arr, tr, edge_keys, exs, eys,
                                  threshold_m=threshold_m,
                                  is_bridge=is_bridge,
                                  bridge_threshold_m=bridge_threshold_m)
        comp_of: dict[object, int] = {}
        safe_components: set[int] = set()
        for ci, comp in enumerate(nx.connected_components(G_cut.to_undirected())):
            for node in comp:
                comp_of[node] = ci
            if not safe_nodes_t.isdisjoint(comp):
                safe_components.add(ci)

        for vi, vnode in enumerate(village_nodes):
            if not road_found[vi] or not baseline_connected[vi] or not np.isnan(isolation_t[vi]):
                continue
            ci = comp_of.get(vnode)
            if ci is None or ci not in safe_components:
                isolation_t[vi] = t_s
                logger.info("  %s isolated at T+%.0f min",
                            village_gdf.iloc[vi].get("village_name", vi), t_s / 60.0)

    # Classify each village: NO_ROAD_DATA (no snapped node) > OSM_GAP (baseline
    # graph gap, not the flood's fault) > FLOOD_ISOLATED (cut off by flooding
    # within the window) > NOT_ISOLATED (never cut off).
    isolation_status = np.full(n_villages, "NOT_ISOLATED", dtype=object)
    isolation_status[~road_found] = "NO_ROAD_DATA"
    isolation_status[road_found & ~baseline_connected] = "OSM_GAP"
    isolation_status[road_found & baseline_connected & ~np.isnan(isolation_t)] = "FLOOD_ISOLATED"

    result = village_gdf.copy()
    result["road_nodes_found"]      = road_found
    result["baseline_connected"]    = baseline_connected
    result["n_road_exits"]          = n_road_exits
    result["isolation_status"]      = isolation_status
    result["isolation_time_s"]      = isolation_t
    result["water_arrival_time_s"]  = water_arr_t
    # Window = time between losing the road and the water arriving. Positive
    # means people still have a way out after the road is cut upstream.
    result["evacuation_window_s"]   = water_arr_t - isolation_t
    result["isolation_time_min"]    = np.round(isolation_t / 60.0, 1)
    result["water_arrival_min"]     = np.round(water_arr_t / 60.0, 1)
    result["evacuation_window_min"] = np.round(result["evacuation_window_s"] / 60.0, 1)

    n_isolated = int((~np.isnan(isolation_t)).sum())
    n_noroad = int((~road_found).sum())
    logger.info("Isolation complete: %d / %d villages isolated within the window"
                "%s", n_isolated, n_villages,
                f"; {n_noroad} have no road in the graph" if n_noroad else "")
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Road-cut timeline export  ★ THE DIFFERENTIATOR MADE VISIBLE ★
# ──────────────────────────────────────────────────────────────────────────────

def edge_cut_times(
    G: nx.MultiDiGraph,
    raster_stack: Sequence[tuple[float, str | Path]],
    threshold_m: float = THRESH_CAR,
    bridge_threshold_m: float = 3.0,
    spacing_m: Optional[float] = None,
) -> tuple[nx.MultiDiGraph, list[tuple], np.ndarray, np.ndarray]:
    """
    First timestep [s] at which each edge floods.

    The one implementation behind the road timeline, the backfill and evacuation
    routing. Returns ``(G_proj, edge_keys, cut_time_s, is_bridge)``; ``cut_time_s``
    is NaN where the edge is never cut. ``edge_keys`` are ``(u, v, k)`` and match
    ``G``'s own keys (projection keeps node ids and keys).

    ``spacing_m=None`` samples the edge MIDPOINT only (the road timeline's
    convention). With a spacing, the edge is sampled every ``spacing_m`` along its
    geometry, both ends included, and floods when its DEEPEST sample does. Routing
    must use a spacing: a long link with a dry midpoint can still run through
    3.6 m of water (measured on annamayya_compound, 2026-09-24).
    """
    if not _OSMNX:
        raise RuntimeError("OSMnx not installed. Run: pip install osmnx")
    if not raster_stack:
        raise ValueError("raster_stack is empty")
    with rasterio.open(raster_stack[0][1]) as src0:
        flood_crs = src0.crs
    G_proj = ox.project_graph(G, to_crs=flood_crs)
    edge_keys, exs, eys, is_bridge = edge_midpoints(G_proj)
    logger.info("Road cut times: %d edges over %d timesteps", len(edge_keys), len(raster_stack))

    owner = np.arange(len(edge_keys))            # sample -> edge index
    if spacing_m:
        sx, sy, so = [], [], []
        for i, (u, v, k) in enumerate(edge_keys):
            geom = G_proj.edges[u, v, k].get("geometry")
            if geom is None:
                a, b = G_proj.nodes[u], G_proj.nodes[v]
                pts = np.linspace(0.0, 1.0, max(2, int(np.hypot(b["x"] - a["x"], b["y"] - a["y"]) // spacing_m) + 2))
                sx.extend(a["x"] + (b["x"] - a["x"]) * pts); sy.extend(a["y"] + (b["y"] - a["y"]) * pts)
            else:
                n = max(2, int(geom.length // spacing_m) + 2)
                for d in np.linspace(0.0, geom.length, n):
                    p = geom.interpolate(d)
                    sx.append(p.x); sy.append(p.y)
            so.extend([i] * (len(sx) - len(so)))
        exs, eys, owner = np.asarray(sx), np.asarray(sy), np.asarray(so)

    cut_time_s = np.full(len(edge_keys), np.nan)
    eff_thresh = np.where(is_bridge, bridge_threshold_m, threshold_m)
    for t_s, raster_path in raster_stack:
        with rasterio.open(raster_path) as src:
            depth_arr = src.read(1).astype(float)
            tr = src.transform
        depths = np.nan_to_num(sample_raster(depth_arr, tr, exs, eys), nan=0.0)
        edge_max = np.zeros(len(edge_keys))
        np.maximum.at(edge_max, owner, depths)
        newly_cut = (edge_max >= eff_thresh) & np.isnan(cut_time_s)
        cut_time_s[newly_cut] = t_s
    return G_proj, edge_keys, cut_time_s, is_bridge


def emit_road_cut_timeline(
    G: nx.MultiDiGraph,
    raster_stack: Sequence[tuple[float, str | Path]],
    out_path: str | Path,
    threshold_m: float = THRESH_CAR,
    bridge_threshold_m: float = 3.0,
) -> Path:
    """
    Write ``roads_timeline.geojson`` — one feature per road link, carrying
    the first simulation timestep at which it becomes impassable.

    This materialises the road-cut geometry that :func:`compute_isolation_times`
    computes per timestep and previously discarded. Rendering this file on the
    map turns the claimed differentiator — "time-varying road-graph cutting" —
    from prose in the About tab into a visible layer.

    Parameters
    ----------
    G            : Full (uncut) OSMnx road graph, WGS84.
    raster_stack : ``[(t_seconds, depth_raster_path), ...]`` in time order.
    out_path     : Where to write ``roads_timeline.geojson``.
    threshold_m  : Depth at which an edge is impassable (default THRESH_CAR).

    Returns
    -------
    Path to the written GeoJSON file.

    GeoJSON feature properties
    --------------------------
    ``cut_time_min``   : float | null — first timestep the edge depth >= threshold;
                         null = never cut in this simulation window.
    ``cut_time_s``     : float | null — same in seconds.
    ``is_bridge``      : bool
    ``highway``        : str — OSM highway tag, e.g. "primary", "track"
    ``cut_threshold_m``: float — the depth threshold used (cited on screen)
    """
    if not _OSMNX:
        raise RuntimeError("OSMnx not installed. Run: pip install osmnx")
    if not raster_stack:
        raise ValueError("raster_stack is empty")

    out_path = Path(out_path)
    G_proj, edge_keys, cut_time_s, is_bridge = edge_cut_times(
        G, raster_stack, threshold_m, bridge_threshold_m)
    flood_crs = G_proj.graph["crs"]
    n_edges = len(edge_keys)

    n_cut = int((~np.isnan(cut_time_s)).sum())
    logger.info("Road timeline: %d / %d edges cut within the window", n_cut, n_edges)

    # Reproject node positions to WGS84 once for geometry output
    import pyproj
    to_wgs84_tr = pyproj.Transformer.from_crs(flood_crs, "EPSG:4326", always_xy=True)

    features = []
    for i, (u, v, k) in enumerate(edge_keys):
        edge_data = G_proj.edges[u, v, k]
        geom = edge_data.get("geometry")

        if geom is not None:
            # Transform the existing geometry from UTM to WGS84
            wgs84_coords = [
                to_wgs84_tr.transform(x, y)
                for x, y in geom.coords
            ]
        else:
            # Fallback: straight line between node centres
            ux = G_proj.nodes[u]["x"]; uy = G_proj.nodes[u]["y"]
            vx = G_proj.nodes[v]["x"]; vy = G_proj.nodes[v]["y"]
            wgs84_coords = [
                to_wgs84_tr.transform(ux, uy),
                to_wgs84_tr.transform(vx, vy),
            ]

        ct_s = None if np.isnan(cut_time_s[i]) else float(cut_time_s[i])
        ct_min = None if ct_s is None else round(ct_s / 60.0, 1)

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                # 6 decimal places is ~0.11 m at the equator -- far finer than a
                # 30 m DEM cell can justify, and it roughly halves the file.
                # Full float64 coordinates were the bulk of a 3.6 MB payload for
                # the Derna graph (10,591 links), which the browser then has to
                # parse and tile on every load.
                "coordinates": [[round(lon, 6), round(lat, 6)]
                                for lon, lat in wgs84_coords],
            },
            "properties": {
                "cut_time_s":      ct_s,
                "cut_time_min":    ct_min,
                "is_bridge":       bool(is_bridge[i]),
                "highway":         edge_data.get("highway", "unknown"),
                "cut_threshold_m": bridge_threshold_m if is_bridge[i] else threshold_m,
                "osmid":           str(edge_data.get("osmid", "")),
            },
        })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        import json
        json.dump({"type": "FeatureCollection", "features": features}, f)
    logger.info("Road timeline written → %s  (%d features, %d cut)",
                out_path, len(features), n_cut)
    return out_path
