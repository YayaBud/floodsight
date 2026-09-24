"""
M6 — Time-aware evacuation routing
==================================

For every settlement, and for a departure time *t*: the fastest way to the **dry
mainland** over roads that are still open when the evacuee reaches them, in two
modes, on foot (primary: most evacuees have no vehicle) and by vehicle.

Rules, all pinned by ``tests/test_evacuation.py``:

* A road link is usable only if the evacuee is **off it before it floods**:
  ``arrival(u) + tau(u, v) < cut(u, v)``. Links only ever close, so waiting never
  helps and an earliest-arrival Dijkstra is exact.
* Feasibility can only shrink as departure gets later, so the latest safe
  departure ("leave by") is found by bisection, not by scanning.
* The destination is the **dry mainland**: the largest connected cluster of road
  nodes that never get wet, joined by links that are never cut. The nearest dry
  node alone is often an island the flood surrounds. Measured on
  ``annamayya_compound``: 1,911 dry nodes in 47 clusters, 46 of them islands.
* Roads are undirected for evacuees; one-way rules do not hold in an evacuation.
* Speeds are defaults, not measurements. The OSM graphs carry no speed tags, so a
  vehicle uses a per-road-class default and a walker 4.5 km/h everywhere. Debris
  and partial blockage are not modelled. Every output says so.
"""
from __future__ import annotations

import heapq
import math
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

WALK_KPH = 4.5
VEHICLE_KPH = {
    "trunk": 60.0, "primary": 50.0, "secondary": 40.0, "tertiary": 30.0,
    "unclassified": 25.0, "residential": 20.0, "living_street": 10.0,
}
VEHICLE_FALLBACK_KPH = 20.0
MODES = ("foot", "vehicle")

METHOD_NOTE = (
    "Earliest-arrival routing over the OSM road graph; a link is usable only if the "
    "evacuee leaves it before this run floods it (>= 0.30 m anywhere along it, sampled "
    "every 25 m; bridges assume 3.0 m deck clearance). Destination: the largest cluster of road nodes and links this run never "
    "wets. Speeds are defaults, not measurements: 4.5 km/h on foot; by vehicle a "
    "per-road-class default. Debris and partial blockage are not modelled."
)


def _speed_kph(data: dict, mode: str) -> float:
    if mode == "foot":
        return WALK_KPH
    sp = data.get("speed_kph")
    if sp:
        try:
            return float(sp[0] if isinstance(sp, list) else sp)
        except (TypeError, ValueError):
            pass
    hw = data.get("highway")
    hw = hw[0] if isinstance(hw, list) else hw
    return VEHICLE_KPH.get(hw, VEHICLE_FALLBACK_KPH)


def build_adjacency(G, cut_min: dict, mode: str) -> dict:
    """``{node: [(nbr, tau_min, cut_min, edge_key, forward), ...]}``, undirected.

    ``cut_min`` maps ``(u, v, k)`` to the minute the link floods, or None/absent
    for never.
    """
    adj = {n: [] for n in G.nodes}
    for u, v, k, d in G.edges(keys=True, data=True):
        tau = float(d.get("length", 0.0)) / 1000.0 / _speed_kph(d, mode) * 60.0
        c = cut_min.get((u, v, k))
        c = math.inf if c is None else float(c)
        adj[u].append((v, tau, c, (u, v, k), True))
        adj[v].append((u, tau, c, (u, v, k), False))
    return adj


def mainland(G, never_wet: set, cut_min: dict) -> set:
    """Largest connected set of never-wet nodes joined by never-cut links."""
    nbrs = {n: [] for n in never_wet}
    for u, v, k in G.edges(keys=True):
        if u in nbrs and v in nbrs and cut_min.get((u, v, k)) is None:
            nbrs[u].append(v)
            nbrs[v].append(u)
    best, seen = set(), set()
    for s in nbrs:
        if s in seen:
            continue
        comp, stack = {s}, [s]
        seen.add(s)
        while stack:
            for w in nbrs[stack.pop()]:
                if w not in seen:
                    seen.add(w)
                    comp.add(w)
                    stack.append(w)
        if len(comp) > len(best):
            best = comp
    return best


def earliest_arrival(adj: dict, origin, t_dep: float, targets: set):
    """Fastest way from ``origin`` into ``targets`` leaving at ``t_dep``.

    Returns ``(arrival_min, nodes, steps)`` with
    ``steps = [(edge_key, forward, tau_min, cut_min), ...]``, or None when every way
    out floods first.
    """
    if origin in targets:
        return t_dep, [origin], []
    best = {origin: t_dep}
    prev = {}
    heap = [(t_dep, 0, origin)]
    tie = 1
    while heap:
        t, _, u = heapq.heappop(heap)
        if t > best[u]:
            continue
        if u in targets:
            nodes, steps = [u], []
            while u in prev:
                p, ek, fwd, tau, cut = prev[u]
                steps.append((ek, fwd, tau, cut))
                nodes.append(p)
                u = p
            return t, nodes[::-1], steps[::-1]
        for v, tau, cut, ek, fwd in adj[u]:
            ta = t + tau
            if ta >= cut:            # still on the link when it floods
                continue
            if ta < best.get(v, math.inf):
                best[v] = ta
                prev[v] = (u, ek, fwd, tau, cut)
                heapq.heappush(heap, (ta, tie, v))
                tie += 1
    return None


def leave_by(adj: dict, origin, targets: set, t_lo: float, t_hi: float,
             tol: float = 1.0) -> tuple[str, Optional[float]]:
    """Latest departure in ``[t_lo, t_hi]`` that still reaches ``targets``.

    ``("open", None)``: a way out stays open to ``t_hi``.
    ``("none", None)``: no way out even at ``t_lo``.
    ``("leave_by", t)``: ``t`` is feasible and ``t + tol`` is not.
    """
    if earliest_arrival(adj, origin, t_hi, targets) is not None:
        return "open", None
    if earliest_arrival(adj, origin, t_lo, targets) is None:
        return "none", None
    lo, hi = t_lo, t_hi                       # lo feasible, hi infeasible
    while hi - lo > tol:
        mid = (lo + hi) / 2.0
        if earliest_arrival(adj, origin, mid, targets) is not None:
            lo = mid
        else:
            hi = mid
    return "leave_by", lo


def _nearest_nodes(G, lons: Iterable[float], lats: Iterable[float]):
    """Nearest graph node and its distance [m] for each point.
    ponytail: equirectangular metres, fine at the km scale of one scenario; use a
    projected KD-tree if a domain ever spans hundreds of km."""
    ids = list(G.nodes)
    nx_ = np.array([G.nodes[n]["x"] for n in ids])
    ny_ = np.array([G.nodes[n]["y"] for n in ids])
    k = math.cos(math.radians(float(np.mean(ny_)))) * 111_320.0
    out = []
    for lon, lat in zip(lons, lats):
        d2 = ((nx_ - lon) * k) ** 2 + ((ny_ - lat) * 110_540.0) ** 2
        i = int(np.argmin(d2))
        out.append((ids[i], float(math.sqrt(d2[i]))))
    return out


def dry_points(max_depth_path: str | Path, lons, lats, threshold_m: float) -> list[bool]:
    """True where the run-maximum depth at (lon, lat) stays below ``threshold_m``."""
    import rasterio
    from pyproj import Transformer
    from ..rasterutils import sample_raster

    with rasterio.open(max_depth_path) as src:
        arr = src.read(1).astype(float)
        if src.nodata is not None:
            arr = np.where(arr == src.nodata, 0.0, arr)
        tr, crs = src.transform, src.crs
    xs, ys = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(
        np.asarray(lons, float), np.asarray(lats, float))
    d = sample_raster(arr, tr, np.atleast_1d(xs), np.atleast_1d(ys))
    return [not (v >= threshold_m) for v in d]


def shelters_from_gdf(facilities_gdf, max_depth_path, threshold_m: float) -> list[dict]:
    """OSM hospitals/schools whose own location stays dry in this run."""
    if facilities_gdf is None or not len(facilities_gdf):
        return []
    g = facilities_gdf.to_crs("EPSG:4326") if facilities_gdf.crs else facilities_gdf
    pts = g.geometry.representative_point()          # some are building polygons
    ok = dry_points(max_depth_path, pts.x, pts.y, threshold_m)
    out = []
    def _text(v):          # GeoPandas gives NaN for a missing name, and NaN is truthy
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else str(v)
    for (_, r), p, dry in zip(g.iterrows(), pts, ok):
        if dry:
            am = _text(r.get("amenity"))
            out.append({"name": _text(r.get("name")) or am or "shelter", "amenity": am,
                        "lon": p.x, "lat": p.y})
    return out


def never_wet_nodes(G, max_depth_path: str | Path, threshold_m: float) -> set:
    """Nodes whose run-maximum depth stays below ``threshold_m``."""
    import rasterio
    from pyproj import Transformer
    from ..rasterutils import sample_raster

    with rasterio.open(max_depth_path) as src:
        arr = src.read(1).astype(float)
        if src.nodata is not None:
            arr = np.where(arr == src.nodata, 0.0, arr)
        tr, crs = src.transform, src.crs
    ids = list(G.nodes)
    to_r = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    xs, ys = to_r(np.array([G.nodes[n]["x"] for n in ids]), np.array([G.nodes[n]["y"] for n in ids]))
    d = sample_raster(arr, tr, np.asarray(xs), np.asarray(ys))
    return {n for n, v in zip(ids, d) if not (v >= threshold_m)}


def _shelter_index(G, adj: dict, main: set, shelter_nodes: dict) -> dict:
    """Multi-source Dijkstra inside the mainland from every shelter node:
    ``{node: (minutes, shelter)}`` for the nearest shelter by travel time."""
    best, heap, tie = {}, [], 0
    for node, sh in shelter_nodes.items():
        best[node] = (0.0, sh)
        heapq.heappush(heap, (0.0, tie, node))
        tie += 1
    while heap:
        t, _, u = heapq.heappop(heap)
        if t > best[u][0]:
            continue
        for v, tau, cut, _ek, _f in adj[u]:
            if v not in main or cut != math.inf:
                continue
            if t + tau < best.get(v, (math.inf, None))[0]:
                best[v] = (t + tau, best[u][1])
                heapq.heappush(heap, (t + tau, tie, v))
                tie += 1
    return best


def path_last_departure(steps) -> float:
    """Latest departure (exclusive) for which a FIXED path stays usable: the walker
    must leave link i before its cut, so ``t < cut_i - time_to_end_of_link_i``."""
    t, latest = 0.0, math.inf
    for _ek, _fwd, tau, cut in steps:
        t += tau
        latest = min(latest, cut - t)
    return latest


def _path_coords(G, nodes, steps):
    """WGS84 line along the traversed links, each reversed when walked backwards."""
    if not steps:
        n = nodes[0]
        return [[round(G.nodes[n]["x"], 6), round(G.nodes[n]["y"], 6)]] * 2
    coords = []
    for (u, v, k), fwd, _tau, _cut in steps:
        geom = G.edges[u, v, k].get("geometry")
        if geom is not None:
            seg = list(geom.coords)
        else:
            seg = [(G.nodes[u]["x"], G.nodes[u]["y"]), (G.nodes[v]["x"], G.nodes[v]["y"])]
        if not fwd:
            seg = seg[::-1]
        if coords:
            seg = seg[1:]
        coords.extend([round(x, 6), round(y, 6)] for x, y in seg)
    return coords


def route_settlements(G, cut_min: dict, never_wet: set, settlements: list[dict],
                      shelters: list[dict], t_lo: float, t_hi: float,
                      ) -> dict:
    """Evacuation routes for every settlement, both modes, every departure.

    ``settlements``: dicts with ``village_id, village_name, priority_rank, lon, lat,
    water_arrival_min``. ``shelters``: dicts with ``name, amenity, lon, lat`` that
    are themselves dry. Returns a GeoJSON FeatureCollection. One feature per
    route per mode, usable for every departure in ``[dep_from_min, dep_to_min]``
    (0.1 min resolution; the next route starts 0.1 min later),
    plus a ``settlements`` summary (leave-by per mode) and the method note.
    """
    main = mainland(G, never_wet, cut_min)
    snodes = _nearest_nodes(G, [s["lon"] for s in shelters], [s["lat"] for s in shelters])
    shelter_nodes = {}
    for sh, (node, dist) in zip(shelters, snodes):
        if node in main and node not in shelter_nodes:
            shelter_nodes[node] = {**sh, "access_m": round(dist)}
    onodes = _nearest_nodes(G, [s["lon"] for s in settlements], [s["lat"] for s in settlements])

    features, summary = [], []
    for s, (origin, access_m) in zip(settlements, onodes):
        row = {k: s.get(k) for k in ("village_id", "village_name", "priority_rank",
                                     "water_arrival_min")}
        row.update(access_m=round(access_m), origin_on_mainland=origin in main, modes={})
        summary.append(row)
    for mode in MODES:
        adj = build_adjacency(G, cut_min, mode)
        sidx = _shelter_index(G, adj, main, shelter_nodes)
        for s, (origin, _), row in zip(settlements, onodes, summary):
            status, lb = leave_by(adj, origin, main, t_lo, t_hi)
            row["modes"][mode] = {"status": status,
                                  "leave_by_min": None if lb is None else round(lb, 1)}
            if status == "none":
                continue
            # Event-driven, exact: links only close, so the best route at t stays
            # best until its own last safe departure; recompute just after it.
            variants, t = [], t_lo              # [(t_from, t_to, result)]
            while t <= t_hi:
                r = earliest_arrival(adj, origin, t, main)
                if r is None:
                    break
                L = min(path_last_departure(r[2]), t_hi + 0.1)
                t_to = min(t_hi, math.floor((L - 1e-9) * 10) / 10)
                variants.append((t, max(t, t_to), r))
                t = round(max(t, t_to) + 0.1, 1)
            if status == "leave_by" and variants:
                # The exact end of the last route window beats the 1-min bisection.
                row["modes"][mode]["leave_by_min"] = variants[-1][1]
            for t_from, t_to, (arr, nodes, steps) in variants:
                dest = nodes[-1]
                sh_t, sh = sidx.get(dest, (None, None))
                length_km = sum(float(G.edges[st[0]].get("length", 0.0)) for st in steps) / 1000.0
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": _path_coords(G, nodes, steps)},
                    "properties": {
                        "village_id": s.get("village_id"), "village_name": s.get("village_name"),
                        "priority_rank": s.get("priority_rank"), "mode": mode,
                        "dep_from_min": round(t_from, 1), "dep_to_min": round(t_to, 1),
                        "travel_min": round(arr - t_from, 1), "length_km": round(length_km, 3),
                        "status": status, "leave_by_min": row["modes"][mode]["leave_by_min"],
                        "dest_lon": round(G.nodes[dest]["x"], 6), "dest_lat": round(G.nodes[dest]["y"], 6),
                        "shelter_name": sh["name"] if sh else None,
                        "shelter_amenity": sh["amenity"] if sh else None,
                        "shelter_extra_min": None if sh_t is None else round(sh_t, 1),
                    },
                })
    return {"type": "FeatureCollection", "features": features, "settlements": summary,
            "mainland_nodes": len(main), "shelters_on_mainland": len(shelter_nodes),
            "method": METHOD_NOTE}
