"""Evacuation routing rules (src/m6_isolation/evacuation.py) on hand-built graphs.

On foot every km takes 60/4.5 = 13.33 min, so every expected time here is by hand.
"""
import math

import networkx as nx
import pytest

from src.m6_isolation.evacuation import (
    build_adjacency, earliest_arrival, leave_by, mainland, route_settlements)

KM = 60.0 / 4.5          # minutes per km on foot


def graph(nodes, edges):
    """nodes: {id: (lon, lat)}; edges: [(u, v, length_m)] -> MultiDiGraph."""
    G = nx.MultiDiGraph()
    for n, (x, y) in nodes.items():
        G.add_node(n, x=x, y=y)
    for u, v, L in edges:
        G.add_edge(u, v, key=0, length=L, highway="residential")
    return G


def diamond():
    # A -1km- B -1km- D ; A -2km- C -2km- D ; D -1km- M  (D, M = dry mainland)
    return graph({"A": (0, 0), "B": (0.01, 0.01), "C": (0.01, -0.01),
                  "D": (0.02, 0), "M": (0.03, 0)},
                 [("A", "B", 1000), ("B", "D", 1000), ("A", "C", 2000),
                  ("C", "D", 2000), ("D", "M", 1000)])


def test_route_replans_when_its_shortest_path_is_cut():
    G = diamond()
    cut = {("A", "B", 0): 30.0}
    adj = build_adjacency(G, cut, "foot")
    t, nodes, _ = earliest_arrival(adj, "A", 0.0, {"D", "M"})
    assert nodes == ["A", "B", "D"] and t == pytest.approx(2 * KM)
    t, nodes, _ = earliest_arrival(adj, "A", 20.0, {"D", "M"})     # A-B would end at 33.3 > 30
    assert nodes == ["A", "C", "D"] and t == pytest.approx(20.0 + 4 * KM)


def test_evacuee_still_on_a_link_when_it_floods_is_refused():
    G = graph({"A": (0, 0), "D": (0.01, 0)}, [("A", "D", 1000)])
    adj = build_adjacency(G, {("A", "D", 0): 10.0}, "foot")        # needs 13.3 min, floods at 10
    assert earliest_arrival(adj, "A", 0.0, {"D"}) is None
    adj = build_adjacency(G, {("A", "D", 0): 14.0}, "foot")
    assert earliest_arrival(adj, "A", 0.0, {"D"}) is not None


def test_a_dry_island_is_never_the_destination():
    # I is dry and 100 m away, but its only link floods; the mainland D-M is 3 km away.
    G = graph({"A": (0, 0), "I": (0.001, 0), "D": (0.03, 0), "M": (0.04, 0)},
              [("A", "I", 100), ("A", "D", 3000), ("D", "M", 1000)])
    cut = {("A", "I", 0): 500.0}
    main = mainland(G, {"I", "D", "M"}, cut)
    assert main == {"D", "M"}
    _, nodes, _ = earliest_arrival(build_adjacency(G, cut, "foot"), "A", 0.0, main)
    assert nodes[-1] == "D"


def test_leave_by_is_the_last_feasible_departure_and_matches_brute_force():
    G = diamond()
    cut = {("A", "B", 0): 30.0, ("A", "C", 0): 90.0}
    adj = build_adjacency(G, cut, "foot")
    status, lb = leave_by(adj, "A", {"D", "M"}, -60.0, 600.0)
    assert status == "leave_by"
    # Last way out is A-C (2 km, 26.7 min) before minute 90 -> leave by 63.3.
    assert lb == pytest.approx(90.0 - 2 * KM, abs=1.0)
    for t in range(-60, 600):
        ok = earliest_arrival(adj, "A", float(t), {"D", "M"}) is not None
        if t <= lb:
            assert ok, t
        elif t >= lb + 1.0:
            assert not ok, t


def test_links_that_never_flood_stay_open():
    G = diamond()
    adj = build_adjacency(G, {("A", "B", 0): None}, "foot")
    assert leave_by(adj, "A", {"D", "M"}, 0.0, 1440.0) == ("open", None)
    G2 = graph({"A": (0, 0), "D": (0.01, 0)}, [("A", "D", 1000)])
    adj2 = build_adjacency(G2, {("A", "D", 0): 5.0}, "foot")
    assert leave_by(adj2, "A", {"D"}, 10.0, 100.0) == ("none", None)


def test_route_settlements_both_modes_and_the_replan_shows_as_two_variants():
    G = diamond()
    cut = {("A", "B", 0): 30.0, ("A", "C", 0): 90.0}
    out = route_settlements(
        G, cut, never_wet={"D", "M"},
        settlements=[{"village_id": "V1", "village_name": "A-town", "priority_rank": 1,
                      "lon": 0.0, "lat": 0.0, "water_arrival_min": 120.0},
                     {"village_id": "V2", "village_name": "M-town", "priority_rank": 2,
                      "lon": 0.03, "lat": 0.0, "water_arrival_min": None}],
        shelters=[{"name": "School", "amenity": "school", "lon": 0.03, "lat": 0.0}],
        t_lo=0.0, t_hi=300.0)
    s1, s2 = out["settlements"]
    assert s1["modes"]["foot"]["status"] == "leave_by"
    assert s1["modes"]["foot"]["leave_by_min"] == pytest.approx(90.0 - 2 * KM, abs=1.0)
    # Vehicle at 20 km/h: A-C 2 km = 6 min, so it can leave later than a walker.
    assert s1["modes"]["vehicle"]["leave_by_min"] > s1["modes"]["foot"]["leave_by_min"]
    foot = [f["properties"] for f in out["features"]
            if f["properties"]["village_id"] == "V1" and f["properties"]["mode"] == "foot"]
    assert len(foot) == 2                              # via B, then via C after A-B floods
    assert foot[0]["length_km"] == pytest.approx(2.0) and foot[1]["length_km"] == pytest.approx(4.0)
    # Via B is usable until 30 - 13.3 = 16.7 (exclusive); via C takes over 0.1 min later.
    assert foot[0]["dep_to_min"] == pytest.approx(16.6)
    assert foot[1]["dep_from_min"] == pytest.approx(16.7)
    assert foot[1]["dep_to_min"] == pytest.approx(s1["modes"]["foot"]["leave_by_min"], abs=1.0)
    assert foot[0]["shelter_name"] == "School"
    # A settlement already on the dry mainland: zero-length, open all run.
    assert s2["origin_on_mainland"] and s2["modes"]["foot"]["status"] == "open"
    assert out["mainland_nodes"] == 2 and out["shelters_on_mainland"] == 1


def test_a_link_with_a_dry_midpoint_but_a_flooded_end_is_cut_when_sampled_along(tmp_path):
    """Midpoint-only sampling let routes walk through 3.6 m of water (2026-09-24)."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from src.m6_isolation.isolation import edge_cut_times

    # 1 km link along the equator, lon 0 -> 0.009; only its west end is under 1 m.
    depth = np.zeros((4, 100), dtype="float32")
    depth[:, :10] = 1.0                # x < 0.0005, i.e. the first ~55 m
    tif = tmp_path / "d.tif"
    with rasterio.open(tif, "w", driver="GTiff", height=4, width=100, count=1, dtype="float32",
                       crs="EPSG:4326", transform=from_origin(-0.0005, 0.0002, 0.0001, 0.0001)) as dst:
        dst.write(depth, 1)
    G = graph({"W": (0.0, 0.0), "E": (0.009, 0.0)}, [("W", "E", 1000)])
    G.graph["crs"] = "EPSG:4326"
    stack = [(600.0, tif)]
    _, _, mid, _ = edge_cut_times(G, stack)
    _, _, along, _ = edge_cut_times(G, stack, spacing_m=25.0)
    assert np.isnan(mid[0])            # midpoint is dry
    assert along[0] == 600.0           # the flooded end cuts it
