import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import numpy as np
import networkx as nx
from affine import Affine

from src.m6_isolation.isolation import cut_flooded_edges

def test_bridge_clearance_filtering():
    """Verify that elevated bridges are preserved under shallow floodwaters and only cut when depth exceeds bridge_threshold_m."""
    G = nx.MultiDiGraph()
    G.add_node(1, x=10.0, y=10.0)
    G.add_node(2, x=20.0, y=10.0)
    G.add_node(3, x=30.0, y=10.0)
    
    # Edge (1, 2) is a normal surface road
    G.add_edge(1, 2, key=0, highway="primary", bridge="no")
    # Edge (2, 3) is an elevated bridge
    G.add_edge(2, 3, key=0, highway="primary", bridge="yes")
    
    edge_keys = [(1, 2, 0), (2, 3, 0)]
    xs = np.array([15.0, 25.0])
    ys = np.array([10.0, 10.0])
    is_bridge = np.array([False, True])
    
    # 100x100 raster with 1.5m flood depth everywhere
    depth_arr = np.full((100, 100), 1.5, dtype=np.float32)
    transform = Affine(1.0, 0, 0, 0, -1.0, 100.0)
    
    # At 1.5m depth, threshold_m=0.3m, bridge_threshold_m=3.0m:
    # Surface road (1, 2) should be CUT (1.5 >= 0.3)
    # Bridge (2, 3) should REMAIN (1.5 < 3.0)
    G_cut = cut_flooded_edges(
        G, depth_arr, transform, edge_keys, xs, ys,
        threshold_m=0.3, is_bridge=is_bridge, bridge_threshold_m=3.0
    )
    assert not G_cut.has_edge(1, 2, 0), "Surface road under 1.5m of water should be cut"
    assert G_cut.has_edge(2, 3, 0), "Bridge with 3.0m clearance under 1.5m of water should NOT be cut"
    
    # Now raise depth to 3.5m: both should be cut
    depth_arr_deep = np.full((100, 100), 3.5, dtype=np.float32)
    G_cut_deep = cut_flooded_edges(
        G, depth_arr_deep, transform, edge_keys, xs, ys,
        threshold_m=0.3, is_bridge=is_bridge, bridge_threshold_m=3.0
    )
    assert not G_cut_deep.has_edge(1, 2, 0), "Surface road should be cut"
    assert not G_cut_deep.has_edge(2, 3, 0), "Bridge submerged at 3.5m should be cut"
