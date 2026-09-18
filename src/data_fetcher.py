"""
FloodSight — Dataset Fetcher
============================
Fetches the real open datasets the pipeline runs on.

Scenarios are the two events SIH26161 names in its own background paragraph:
  1. Phutkal / Phuktal landslide dam, Zanskar, Ladakh — 2015
  2. Rishi Ganga natural lake, Chamoli, Uttarakhand — Feb 2021

Sources
-------
DEM        Copernicus DEM GLO-30 (ESA), 1 arc-second, public AWS S3 mirror.
           No registration, no API key. Read as a windowed COG so only the
           area of interest crosses the network, then mosaicked and
           reprojected to the local UTM zone so the solver works in metres.
Roads      OpenStreetMap via OSMnx.
Villages   OpenStreetMap ``place`` nodes — real settlement names and positions.
Facilities OpenStreetMap ``amenity`` = hospital / clinic / school.
Buildings  OpenStreetMap building footprints (sparse in the high Himalaya;
           that sparsity is real and is reported rather than filled in).

Provenance
----------
``fetch_dem`` returns ``(path, Provenance)``. The synthetic generator is still
here because it makes the demo runnable with no network, but it is reachable
only by explicitly asking for it, and it returns ``SYNTHETIC_TERRAIN`` so every
number derived from it is labelled all the way to the screen.
"""

from __future__ import annotations

import logging
import os
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import geopandas as gpd
import rasterio
from rasterio.merge import merge as rio_merge
from rasterio.transform import from_bounds
from shapely.geometry import Point

from .provenance import Provenance

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

# GDAL needs these to read remote COGs efficiently.
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_USE_HEAD", "NO")
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "3")
os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "2")

_COP_S3 = "https://copernicus-dem-30m.s3.amazonaws.com"


SCENARIOS = {
    "phutkal": {
        # flow_regime: which physics class this event belongs to. Phuktal 2015: outburst of a landslide-dam lake through a gravel gorge. Sediment-
        # laden, but the documented damage (bridges, banks, canals) is consistent with a
        # water-dominated flood rather than a granular flow. SWE is an approximation here,
        # biased low on depth and momentum.
        "flow_regime": "hyperconcentrated",
        "name": "Phutkal River Landslide Dam (2015)",
        "lat": 33.25242, "lon": 77.05783,
        "bbox": (76.75, 33.12, 77.10, 33.40),      # west, south, east, north
        "utm_epsg": 32643,                          # UTM 43N
        # wse_m / dam_height_m sourced 2026-09-13. The deposit was measured at 69 m
        # from Cartosat-2 stereo imagery (Landslides 14:529-538), not 58 m. The DEM
        # foundation under the blockage is 3736.11 m, so the crest is 3805.11 m.
        # Cross-check: the conditioned DEM impounds 27.05 MCM at that crest and
        # 31.38 MCM at 3810 m, against a documented >30 MCM -- the two independent
        # observables (deposit height, impounded volume) agree within ~5 m of height.
        # See findings_results.md 2026-09-13 for the stage-storage table.
        "wse_m": 3805.11,
        "thalweg_m": 3736.11,
        "breach_lon": 77.05783, "breach_lat": 33.25242,
        "dam_height_m": 69.0,
        "volume_mcm": 30.0,
        "event_type": "natural_lake_formation",
        "lake_formation": {
            "trigger": "Massive limestone cliff collapse damming Tsarap Chu gorge near Marshun",
            "formation_time_h": 72.0,
            "length_km": 15.0,
            "type": "Canyon Landslide Dam (15 km impounded lake, 30 MCM)",
        },
        "downstream_structure": {
            "name": "Phuktal Monastery & Tsarap Footbridges",
            "lat": 33.2680, "lon": 77.1770,
            "distance_km": 12.0,
            "status": "Suspension footbridges & riverside hermitages washed away",
            "role": "Downstream infrastructure destruction",
        },
        "failure_trigger": "Progressive crest overtopping through artificial diversion trench",
    },
    "rishiganga": {
        # flow_regime: which physics class this event belongs to. Chamoli 7 Feb 2021: ~27e6 m3 of rock and glacier ice detached from Ronti Peak and
        # became a debris flow carrying >20 m boulders, scouring valley walls to 220 m
        # above the floor. The debris-dammed lake formed AFTER the flood. Clear-water SWE
        # is not the governing model for this event; see m3_breach.DEBRIS_FLOW_MISSING_PHYSICS.
        "flow_regime": "debris_flow",
        "name": "Rishi Ganga Natural Lake & Cascade (Feb 2021)",
        "lat": 30.46915, "lon": 79.71228,
        "bbox": (79.60, 30.38, 79.85, 30.58),
        "utm_epsg": 32644,                          # UTM 44N
        "wse_m": 2450.0,
        "thalweg_m": 2380.0,
        "breach_lon": 79.71228, "breach_lat": 30.46915,
        "dam_height_m": 70.0,
        "volume_mcm": 15.0,
        "event_type": "natural_lake_formation_and_cascade",
        "lake_formation": {
            "trigger": "Ronti Peak rock-ice avalanche detached from 5,500m crashing down Raunthi Gad",
            "formation_time_h": 6.0,
            "length_km": 0.8,
            "type": "High-altitude rock-ice avalanche debris barrier",
        },
        "upstream_trigger": {
            "name": "Ronti Peak Avalanche Scar",
            "lat": 30.3750, "lon": 79.7310,
            "elevation_m": 5500.0,
            "status": "27 MCM rock and glacier ice wedge detached",
            "role": "Disaster trigger",
        },
        "midstream_structure": {
            "name": "Rishiganga Small Hydro Project (HEP)",
            "lat": 30.47827, "lon": 79.69929,
            "distance_km": 4.5,
            "status": "Completely obliterated at T+10 min",
            "role": "Cascade HEP destruction",
        },
        "downstream_structure": {
            "name": "Tapovan Vishnugad HEP Barrage (NTPC)",
            "lat": 30.49338, "lon": 79.62778,
            "distance_km": 11.2,
            "status": "Barrage destroyed & headrace tunnel inundated at T+35 min",
            "role": "Downstream cascade impact",
        },
        "cascade_arrival_min": 35.0,
        "failure_trigger": "Avalanche debris dam overtopping & catastrophic outburst",
        # ── Generic cascade routing config ────────────────────────────────────
        # CLASSIFICATION LEGEND:
        #   upstream.peak_q_m3s: RECONSTRUCTION from ~27 MCM rock-ice wedge + entrainment
        #   routing: ASSUMPTION (high-gradient gorge, very fast celerity)
        #   reservoir: RECONSTRUCTION (Rishiganga HEP small pondage, ~0.15 MCM)
        #   breach_ensemble: MODEL RECONSTRUCTION
        "cascade": {
            "scenario_key": "rishiganga",
            "upstream": {
                "peak_q_m3s":  25000.0,    # RECONSTRUCTION: 27 MCM avalanche outburst
                "base_q_m3s":    200.0,    # February river base flow
                "duration_s":   2400.0,    # ~40 min pulse (steep gorge, fast pass)
                "lead_time_s":   600.0,    # 10 min before Rishiganga HEP hit
                "classification": "RECONSTRUCTION",
            },
            "routing": {
                "k_s":           420.0,    # ASSUMPTION: 4.5 km steep gorge, K ~ 7 min
                "x":               0.30,   # steep gradient -> higher X
                "classification": "ASSUMPTION",
            },
            "reservoir": {
                "z_bed_m":      2380.0,    # thalweg at Rishiganga HEP intake
                "z_frl_m":      2392.0,    # RECONSTRUCTION: small pondage ~0.15 MCM
                "z_crest_m":    2393.0,    # RECONSTRUCTION: concrete weir crest
                "v_frl_mcm":       0.15,   # RECONSTRUCTION: run-of-river HEP pondage
                "alpha_exp":       2.5,
                "classification": "RECONSTRUCTION",
            },
            "spillway": {
                "cd":              1.80,   # concrete ogee spillway
                "length_m":       20.0,
                "z_crest_m":    2392.5,
                "max_q_m3s":    2000.0,   # ASSUMPTION: small HEP rated capacity
            },
            "catchment_runoff": {
                "base_m3s":      200.0,
                "peak_m3s":       0.0,    # no significant catchment runoff in Feb
                "peak_offset_s":   0.0,
                "sigma_s":      5400.0,
            },
            "breach_ensemble": {
                "optimistic":  {"width_m":  10.0, "formation_s": 120.0, "peak_q_m3s": 18000.0,
                                "classification": "MODEL RECONSTRUCTION",
                                "source": "Froehlich (2008) lower — concrete weir overtopping"},
                "central":     {"width_m":  20.0, "formation_s":  90.0, "peak_q_m3s": 25000.0,
                                "classification": "MODEL RECONSTRUCTION",
                                "source": "Froehlich (2008) best estimate — HEP obliteration"},
                "pessimistic": {"width_m":  30.0, "formation_s":  60.0, "peak_q_m3s": 35000.0,
                                "classification": "MODEL RECONSTRUCTION",
                                "source": "Froehlich (2008) upper — full structural failure"},
            },
        },
    },
    # ──────────────────────────────────────────────────────────────────────────
    # OUT-OF-SCOPE-NON-INDIAN — commented out 2026-09-13 at the user's request.
    #
    # derna (Libya), malpasset (France) and ivanovo (Bulgaria) are not Indian
    # events and are out of the current scope. They are COMMENTED, not deleted,
    # because two of them carry assets nothing else replaces:
    #
    #   derna     — the only scenario with a certifiable observation
    #               (Copernicus EMS EMSR696). It is the sole entry left in
    #               m10_validation.observed.SOURCES, so with it commented out
    #               M10 has no event to score any run against.
    #   malpasset — the dam-break numerical benchmark. Its validation data and
    #               the /api/benchmarks/malpasset endpoint read files directly
    #               and do NOT go through SCENARIOS, so the benchmark still
    #               works with the scenario commented out.
    #   ivanovo   — carries nothing; its 'observation' was fabricated and was
    #               already deleted.
    #
    # To restore: grep for OUT-OF-SCOPE-NON-INDIAN here and in tests/, and uncomment.
    # Nothing else was changed — observed.py, the Malpasset benchmark data and
    # the geometry manifests under data/geometry/ are all left in place.
    # ──────────────────────────────────────────────────────────────────────────
#    "derna": {
#        # flow_regime: which physics class this event belongs to. Derna 2023: two engineered embankment dams into a wadi. Clear-water dam break.
#        "flow_regime": "clear_water",
#        "name": "Derna Dams — Abu Mansour + Al-Bilad, Libya (Sep 2023)",
#        "lat": 32.65755, "lon": 22.57733,
#        "bbox": (22.50, 32.60, 22.72, 32.82),
#        "utm_epsg": 32634,                          # UTM 34N
#        "wse_m": 170.0,
#        "thalweg_m": 96.0,
#        "breach_lon": 22.57733, "breach_lat": 32.65755,
#        "dam_height_m": 74.0,
#        "volume_mcm": 22.5,
#        "event_type": "cascading_dam_break",
#        "upstream_structure": {
#            "name": "Abu Mansour Dam (Upstream)",
#            "lat": 32.65755, "lon": 22.57733,
#            "height_m": 74.0, "volume_mcm": 22.5,
#            "status": "Overtopped and burst at 02:30 AM",
#            "role": "Primary upstream breach",
#        },
#        "downstream_structure": {
#            "name": "Al-Bilad Dam (Downstream City Dam)",
#            "lat": 32.75237, "lon": 22.63058,
#            "height_m": 45.0, "volume_mcm": 1.5,
#            "distance_km": 13.7,
#            "status": "Overtopped and destroyed at 03:00 AM (T+30 min)",
#            "role": "Secondary cascading failure into Derna city",
#        },
#        "cascade_arrival_min": 30.0,
#        "failure_trigger": "Storm Daniel precipitation + upstream reservoir overtopping",
#        # ── Generic cascade routing config ────────────────────────────────────
#        # CLASSIFICATION LEGEND:
#        #   upstream.peak_q_m3s: RECONSTRUCTION from 22.5 MCM / ~30min formation
#        #   routing: ASSUMPTION (flash-flood steep wadi, shorter K than natural river)
#        #   reservoir: OFFICIAL ESTIMATE from UNEP / Copernicus post-event survey
#        #   breach_ensemble: MODEL RECONSTRUCTION (Froehlich 2008)
#        "cascade": {
#            "scenario_key": "derna",
#            "upstream": {
#                "peak_q_m3s":   8000.0,     # RECONSTRUCTION: Abu Mansour 22.5 MCM rapid release
#                "base_q_m3s":    100.0,     # wadi base flow during Storm Daniel
#                "duration_s":   3600.0,     # ~60 min surge (steep wadi, fast emptying)
#                "lead_time_s":  1800.0,     # 30 min before downstream breach
#                "classification": "RECONSTRUCTION",
#            },
#            "routing": {
#                "k_s":          1200.0,     # ASSUMPTION: 13.7 km steep wadi ~K=20 min
#                "x":               0.15,
#                "classification": "ASSUMPTION",
#            },
#            "reservoir": {
#                "z_bed_m":        96.0,     # OFFICIAL ESTIMATE: Al-Bilad thalweg
#                "z_frl_m":       135.0,     # ASSUMPTION: estimated FRL from dam height
#                "z_crest_m":     141.0,     # OFFICIAL ESTIMATE: Al-Bilad crest
#                "v_frl_mcm":       1.5,     # OFFICIAL ESTIMATE: Al-Bilad capacity
#                "alpha_exp":       2.5,
#                "classification": "OFFICIAL ESTIMATE",
#            },
#            "spillway": {
#                "cd":              2.00,
#                "length_m":       30.0,
#                "z_crest_m":     138.0,
#                "max_q_m3s":    1200.0,     # ASSUMPTION: small spillway capacity
#            },
#            "catchment_runoff": {
#                "base_m3s":       50.0,
#                "peak_m3s":     3500.0,     # RECONSTRUCTION: Storm Daniel wadi catchment
#                "peak_offset_s": -900.0,
#                "sigma_s":      3600.0,
#            },
#            "breach_ensemble": {
#                "optimistic":  {"width_m": 20.0, "formation_s": 1200.0, "peak_q_m3s":  5000.0,
#                                "classification": "MODEL RECONSTRUCTION",
#                                "source": "Froehlich (2008) lower CI — small embankment dam"},
#                "central":     {"width_m": 35.0, "formation_s":  900.0, "peak_q_m3s":  9500.0,
#                                "classification": "MODEL RECONSTRUCTION",
#                                "source": "Froehlich (2008) best estimate — Al-Bilad 1.5 MCM"},
#                "pessimistic": {"width_m": 55.0, "formation_s":  600.0, "peak_q_m3s": 16000.0,
#                                "classification": "MODEL RECONSTRUCTION",
#                                "source": "Froehlich (2008) upper — UNEP estimated >15,000 m3/s surge"},
#            },
#        },
#    },
#    "malpasset": {
#        # flow_regime: which physics class this event belongs to. Malpasset 1959: arch dam onto a dry rocky valley. The canonical clear-water case.
#        "flow_regime": "clear_water",
#        "name": "Malpasset Arch Dam — France (1959, Canonical Benchmark)",
#        "lat": 43.51216, "lon": 6.75684,
#        "bbox": (6.65, 43.41, 6.82, 43.55),
#        "utm_epsg": 32632,                          # UTM 32N
#        "wse_m": 101.5,
#        "thalweg_m": 35.0,
#        "breach_lon": 6.75684, "breach_lat": 43.51216,
#        "dam_height_m": 66.5,
#        "volume_mcm": 50.0,
#        "downstream_structure": {
#            "name": "Bozon Highway Bridge & Frejus Estuary",
#            "lat": 43.4350, "lon": 6.7420,
#            "distance_km": 8.0,
#            "status": "Bridge destroyed; Frejus submerged in 20 min",
#            "role": "Downstream coastal inundation",
#        },
#    },
#    "ivanovo": {
#        # flow_regime: which physics class this event belongs to. Ivanovo 2012: small embankment dam into a lowland floodplain.
#        "flow_regime": "clear_water",
#        "name": "Ivanovo Dam — Bulgaria (2012, GFD Observed)",
#        "lat": 41.86243, "lon": 25.85388,
#        "bbox": (25.75, 41.78, 26.05, 41.96),
#        "utm_epsg": 32635,                          # UTM 35N
#        "wse_m": 171.0,
#        "thalweg_m": 155.0,
#        "breach_lon": 25.85388, "breach_lat": 41.86243,
#        "dam_height_m": 16.0,
#        "volume_mcm": 2.5,
#        "downstream_structure": {
#            "name": "Biser Village Bridge & Dykes",
#            "lat": 41.8833, "lon": 25.8833,
#            "distance_km": 3.8,
#            "status": "Submerged within 20 min of breach",
#            "role": "Downstream village inundation",
#        },
#    },
    "south_lhonak": {
        # flow_regime: which physics class this event belongs to. South Lhonak 2023: moraine-dam GLOF. The surge transported boulders and moraine
        # and destroyed a 60 m concrete-face dam 42 km downstream. Granular phase is not
        # incidental to this event, it IS the event.
        "flow_regime": "debris_flow",
        "name": "South Lhonak GLOF & Chungthang Dam — Sikkim (Oct 2023)",
        "lat": 27.91478, "lon": 88.18742,
        "bbox": (88.15, 27.50, 88.75, 28.00),
        "utm_epsg": 32645,                          # UTM 45N
        "wse_m": 5200.0,
        "thalweg_m": 5140.0,
        "breach_lon": 88.18742, "breach_lat": 27.91478,
        "dam_height_m": 60.0,
        "volume_mcm": 50.0,
        "event_type": "natural_lake_formation_and_cascade",
        "lake_formation": {
            "trigger": "Ice-rock avalanche into proglacial lake causing moraine dam overtopping",
            "type": "Glacial Lake Outburst Flood (GLOF)",
            "length_km": 2.5,
        },
        "downstream_structure": {
            "name": "Chungthang Dam (Teesta III HEP)",
            "lat": 27.59771, "lon": 88.65025,
            "height_m": 60.0, "distance_km": 42.0,
            "status": "Completely washed away within 10 min of surge arrival (T+75 min)",
            "role": "Downstream dam destruction",
        },
        "cascade_arrival_min": 75.0,
        "failure_trigger": "GLOF wave impact exceeding spillway design flood",
        # ── Generic cascade routing config ────────────────────────────────────
        # CLASSIFICATION LEGEND:
        #   upstream.peak_q_m3s: RECONSTRUCTION from CWI/NDMA post-event reports
        #   routing: RECONSTRUCTION from observed T+75 min travel time
        #   reservoir: OFFICIAL ESTIMATE from NHPC Teesta III project data
        #   breach_ensemble: MODEL RECONSTRUCTION
        "cascade": {
            "scenario_key": "south_lhonak",
            "upstream": {
                "peak_q_m3s":  20000.0,    # RECONSTRUCTION: South Lhonak GLOF outburst
                "base_q_m3s":    300.0,    # Teesta river October base flow
                "duration_s":   7200.0,    # ~2 h GLOF pulse (50 MCM moraine lake)
                "lead_time_s":  4500.0,    # 75 min before Chungthang hit
                "classification": "RECONSTRUCTION",
            },
            "routing": {
                # RECONSTRUCTION: 42 km reach, 75 min observed travel -> K ~ 45 min
                "k_s":          2700.0,
                "x":               0.25,
                "classification": "RECONSTRUCTION",
            },
            "reservoir": {
                # OFFICIAL ESTIMATE: NHPC Teesta III project data
                "z_bed_m":      1050.0,    # Chungthang dam thalweg approx
                "z_frl_m":      1096.0,    # Chungthang FRL
                "z_crest_m":    1100.0,    # Chungthang dam crest
                "v_frl_mcm":      95.0,    # Chungthang gross storage ~95 MCM
                "alpha_exp":       2.5,
                "classification": "OFFICIAL ESTIMATE",
            },
            "spillway": {
                "cd":              2.10,
                "length_m":       80.0,
                "z_crest_m":    1095.0,
                "max_q_m3s":    6000.0,    # OFFICIAL ESTIMATE: design flood spillway
            },
            "catchment_runoff": {
                "base_m3s":      400.0,
                "peak_m3s":     1200.0,    # ASSUMPTION: October monsoon baseflow contribution
                "peak_offset_s": -3600.0,
                "sigma_s":      7200.0,
            },
            "breach_ensemble": {
                "optimistic":  {"width_m":  50.0, "formation_s": 1800.0, "peak_q_m3s": 25000.0,
                                "classification": "MODEL RECONSTRUCTION",
                                "source": "Froehlich (2008) lower CI — Teesta III concrete face dam"},
                "central":     {"width_m": 100.0, "formation_s": 1200.0, "peak_q_m3s": 55000.0,
                                "classification": "MODEL RECONSTRUCTION",
                                "source": "Froehlich (2008) best estimate — NHPC 95 MCM storage"},
                "pessimistic": {"width_m": 180.0, "formation_s":  720.0, "peak_q_m3s": 95000.0,
                                "classification": "MODEL RECONSTRUCTION",
                                "source": "Froehlich (2008) upper — full catastrophic washout"},
            },
        },
    },
    "annamayya": {
        # flow_regime: which physics class this event belongs to. Annamayya 2021: earthen section of an engineered irrigation dam, monsoon flood.
        "flow_regime": "clear_water",
        "name": "Annamayya Dam Failure & Pincha Cascade — Andhra Pradesh (Nov 2021)",
        "lat": 14.21059, "lon": 79.02128,
        # Sized to hold BOTH stages of the routing chain on one grid, so the
        # Stage-1 -> Stage-2 handoff needs no reprojection: south to 13.85 for
        # Pincha (13.90890), north to 14.50 for the Cheyyeru-Pennar confluence
        # (14.4311, 79.1699), east to 79.42 past it.
        # The previous (…, 79.28, 14.36) claimed in its own comment to reach the
        # confluence and did not: it stopped 7.9 km short, and the front stalled
        # on that edge at 47.0 km of a 54.9 km stem in the 24 h run.
        # NOT fully contained, measured 2026-09-13: the HAND-derived corridor
        # layer spans (78.9500, 14.0800, 79.3142, 14.5253), so 2.8 km of it lies
        # north of this edge and 1.1 km west of it. All 23 settlements ARE inside.
        # This is Decision 1 Option A as fixed by the user; the shortfall is
        # recorded, not absorbed.
        "bbox": (78.96, 13.85, 79.42, 14.50),
        "utm_epsg": 32644,                          # UTM 44N
        "wse_m": 206.0,                             # Overtopping crest elevation (+206.000 m MSL, FRL is +203.600 m MSL)
        "thalweg_m": 180.0,                         # Deepest bed / breach invert (+180.000 m MSL)
        # Terrain floor for condition_dem: below this the DEM holds reprojection
        # artefact, not ground. NOT derived from thalweg_m -- that is the DAM-SITE
        # bed, and this domain runs 40 km downstream to the Pennar, where the
        # floodplain genuinely descends to ~64 m. thalweg_m - 100 = 80 m would wall
        # 905 interior cells of real Penna floodplain (surrounding ring median
        # 82.49 m), which is fabricating terrain in the path of the flood.
        # 60.0 is measured, not chosen: at full resolution the cell population steps
        # 29x across it -- [50,60) holds 104 cells, [60,70) holds 3,037 -- and of the
        # 1,722 cells below it, 1,594 sit on the outer 15-cell reprojection frame.
        # The 128 interior ones are a seam at col 1631 (98 cells) and one 5x7 pit at
        # rows 1738-1742 whose own 5x5 neighbourhood has median ~100 m.
        # Measured on the widened bbox, 2026-09-13. Re-measure if the bbox moves.
        "dem_floor_m": 60.0,
        "breach_lon": 79.02128, "breach_lat": 14.21059,
        "dam_height_m": 26.0,                       # Crest - Thalweg (206.0 - 180.0 m)
        "volume_mcm": 63.43,                        # Gross storage at FRL (81.2 MCM at crest overtopping)
        "event_type": "cascading_dam_break",
        # Event clock origin = the DAM FAILURE/WASHOUT, 06:30 IST (EVD-17, MHA
        # D692), per event_clock.origin_iso in annamayya_event_evidence.json and
        # its _decision_record of 2026-09-11. It is NOT overtopping initiation:
        # that is EVD-16, ~05:30-06:00, and sits at t_s = -2,700 s.
        "event_origin_ist": "2021-11-19T06:30:00+05:30",
        "breach_centerline_utm": [
            (286474.9, 1571922.1),
            (286144.5, 1572169.5),
            (286050.0, 1572330.0),
            (285994.0, 1572471.0)
        ],
        "breach_width_m": 130.0,                    # Central hydraulic width (Froehlich 2008)
        "structural_earthen_length_m": 336.0,       # Total washed-out earthen bund section (upper bound)
        "spillway_capacity_m3s": 4136.0,            # 4 operational radial gates (gate #4 jammed)
        "upstream_structure": {
            # 1j: EVD-03 — washout of temporary ring bund, NOT a concrete dam collapse.
            "name": "Pincha Dam Ring Bund (Upstream)",
            "lat": 13.90890, "lon": 78.99956,
            "height_m": 15.0,
            # 1j: volume_mcm 14.0 is the surcharge maximum (gross is 9.28 MCM, EVD-02).
            # Defensible — it was in surcharge when it failed — but noted explicitly.
            "volume_mcm": 14.0,
            "volume_gross_mcm": 9.28,
            # 03:30 IST against the 06:30 T=0 is T-180 min (t_s = -10,800 s,
            # timeline_events.pincha_failure). The old "T-150" label was anchored
            # to the superseded 05:45 origin.
            "status": "Overtopped & ring bund washed out at 03:30 AM (T-180 min)",
            "role": "Upstream feeder ring bund washout triggering Cheyyeru surge",
        },
        "downstream_structure": {
            "name": "Annamayya Dam (Downstream)",
            "lat": 14.21059, "lon": 79.02128,
            "height_m": 26.0, "volume_mcm": 63.43,
            "distance_km": 34.0,
            # Two distinct events, previously conflated into one: overtopping
            # STARTS at 05:45 (EVD-16, t_s = -2,700 s), the bund is fully washed
            # out at 06:30 (EVD-17), and it is the washout that is T=0.
            "status": "Overtopping began 05:45 AM (T-45 min); earthen bund fully washed out 06:30 AM (T=0)",
            "role": "Downstream catastrophic failure",
        },
        # Pincha washout to Annamayya T=0, against the 06:30 origin: 180 min,
        # matching cascade.upstream.lead_time_s = 10,800 s. 150 was the same
        # stale anchor as the T-150 label above.
        "cascade_arrival_min": 180.0,
        "failure_trigger": "Upstream Pincha ring bund washout surge + jammed spillway gate #4",
        # ── 1c: Full cascade config (moved from simulate_annamayya_cascade) ────
        # CLASSIFICATION LEGEND (ANNAMAYYA_DATA_AUDIT.md):
        #   upstream: OFFICIAL ESTIMATE (Pincha surge ~1.40 lakh cusecs, EVD-05)
        #   routing: RECONSTRUCTION (34 km Cheyyeru reach, EVD-06/07)
        #   reservoir: OBSERVED (FRL +203.6m, Crest +206.0m, Bed +180.0m, EVD-08 to EVD-11)
        #   spillway: OBSERVED (4 gates operating, gate #4 jammed, EVD-12/14)
        #   catchment_runoff: derived from EVD-01 rainfall (180mm, IMD/APSDPS)
        #   breach_ensemble:
        #     1h: Three arms based on conflicting official peak inflow figures
        #         MHA 6,412 / CWC 9,065 / IISc 12,740 m³/s (EVD-15).
        #     1g: central formation_s = 1800 (30 min, EVD-16/17 initiation→washout).
        "cascade": {
            "scenario_key": "annamayya",
            # FS-31: ~330 min needed to route the full Cheyyeru corridor from
            # Pincha to the Pennar confluence (EVD-28) -- specific to this
            # reach length, not a generic cascade requirement.
            "min_coverage_duration_s": 20000.0,
            "upstream": {
                "peak_q_m3s": 3964.0, "base_q_m3s": 300.0, "duration_s": 5400.0,
                # P1-2 (2026-09-11): recomputed from the reconciled event
                # clock (data/evidence/annamayya_event_evidence.json,
                # event_clock.origin_iso = 06:30 IST dam washout, EVD-17) --
                # Pincha's OBSERVED failure time is 03:30 IST (EVD-04), which
                # is 10800 s (180 min) before 06:30, not the previous 9000 s
                # (150 min) figure, which was carried over from a "T-150"
                # label that was itself never consistent with either the old
                # or new T=0 anchor (see event_clock._decision_record).
                "lead_time_s": 10800.0, "classification": "OFFICIAL ESTIMATE",
            },
            "routing": {"k_s": 7200.0, "x": 0.20, "classification": "RECONSTRUCTION"},
            "reservoir": {
                "z_bed_m": 180.0, "z_frl_m": 203.6, "z_crest_m": 206.0,
                "v_frl_mcm": 63.43, "alpha_exp": 2.5, "classification": "OBSERVED",
            },
            "spillway": {"cd": 2.15, "length_m": 55.0, "z_crest_m": 189.6, "max_q_m3s": 4136.0},
            # 1i: catchment_runoff now carries classification (EVD-01: 180mm rainfall).
            # Runoff volume ≈ rainfall_depth × catchment_area × C_runoff.
            # Catchment area: MODEL RECONSTRUCTION (DEM-derived).
            # Runoff coefficient: ASSUMED (sensitivity range 0.30–0.60).
            "catchment_runoff": {
                "base_m3s": 800.0, "peak_m3s": 3800.0,
                "peak_offset_s": -1800.0, "sigma_s": 5400.0,
                "classification": "MODEL RECONSTRUCTION",
                "source": "EVD-01: 180mm rainfall (IMD/APSDPS, Jawad precursor depression)",
                "rainfall_mm": 180.0,
                "rainfall_range_mm": [150.0, 240.0],
            },
            "breach_ensemble": {
                # 1h: Arms labelled by source agency (EVD-15).
                # Breach geometry held at central; inflow varied across 3 agencies.
                "optimistic":  {"width_m":  85.0, "formation_s": 4200.0, "peak_q_m3s":  8800.0,
                                "classification": "MODEL RECONSTRUCTION",
                                "source": "Froehlich (2008) lower CI — MHA peak inflow 6,412 m³/s",
                                "agency": "MHA", "agency_peak_inflow_m3s": 6412.0},
                "central":     {"width_m": 130.0, "formation_s": 1800.0, "peak_q_m3s": 12200.0,
                                "classification": "MODEL RECONSTRUCTION",
                                "source": "Froehlich (2008) best estimate — CWC peak inflow 9,065 m³/s",
                                "agency": "CWC", "agency_peak_inflow_m3s": 9065.0},
                "pessimistic": {"width_m": 240.0, "formation_s": 2100.0, "peak_q_m3s": 15500.0,
                                "classification": "MODEL RECONSTRUCTION",
                                "source": "Froehlich (2008) upper envelope — IISc peak inflow 12,740 m³/s (bounded by 336m earthen section)",
                                "agency": "IISc", "agency_peak_inflow_m3s": 12740.0},
            },
        },
    },
}


# ──────────────────────────────────────────────────────────────────────────────
# External scenario definitions — the plug-in-a-new-dam path
# ──────────────────────────────────────────────────────────────────────────────
# `src/scenarios.py` already calls this dict "the legacy parameter table ...
# during migration". Adding a dam by hand-editing a Python literal is what makes
# the framework non-generalisable, which is deliverable (ii). A scenario dropped
# in as JSON is merged here instead, so a new dam needs no code change.
#
# This loader is deliberately thin: it merges parameters only. It grants nothing.
# A new scenario still has to author a geometry manifest with a SOURCED
# `crest_elev_m` and still has to pass `validate_geometry` and gates G1-G5 like
# every built-in one. Dropping a JSON in buys a scenario the right to be
# REFUSED on the record, which is the point.

SCENARIO_DEF_DIR = Path(__file__).resolve().parents[1] / "data" / "scenarios_def"

# What the pipeline reads off a scenario before the geometry gate can even run.
# Missing any of these is a refusal, not a default -- the same rule that removed
# `wse_m + 5.0`. `crest_elev_m` is NOT here: it lives on the geometry manifest,
# where it is gated together with its source and classification.
_SCENARIO_REQUIRED = (
    "name", "lat", "lon", "bbox", "utm_epsg",
    "wse_m", "thalweg_m", "dam_height_m", "volume_mcm",
    "breach_lat", "breach_lon", "event_type", "flow_regime",
)


def _load_external_scenarios(directory: Path | None = None) -> dict[str, dict]:
    """Merge `data/scenarios_def/<key>.json` definitions into SCENARIOS.

    Fail-closed per file: a malformed or incomplete definition is skipped with a
    warning and never half-registered. One bad file must not take down the
    scenarios that are fine, and must not silently become a scenario with
    defaults filled in.
    """
    directory = directory or SCENARIO_DEF_DIR
    out: dict[str, dict] = {}
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        key = path.stem
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("scenario definition %s is unreadable (%s) — skipped", path.name, exc)
            continue
        if not isinstance(cfg, dict):
            logger.warning("scenario definition %s is not an object — skipped", path.name)
            continue
        missing = [f for f in _SCENARIO_REQUIRED if cfg.get(f) is None]
        if missing:
            logger.warning("scenario definition %s is missing %s — skipped, not defaulted",
                           path.name, ", ".join(missing))
            continue
        if key in SCENARIOS:
            logger.warning("scenario definition %s shadows a built-in scenario — skipped",
                           path.name)
            continue
        cfg["bbox"] = tuple(cfg["bbox"])
        cfg.setdefault("definition_source", str(path))
        out[key] = cfg
        logger.info("scenario '%s' registered from %s", key, path.name)
    return out


SCENARIOS.update(_load_external_scenarios())


# ──────────────────────────────────────────────────────────────────────────────
# DEM
# ──────────────────────────────────────────────────────────────────────────────

def _copernicus_tile_url(lat: int, lon: int) -> str:
    """URL of the GLO-30 1x1 degree tile whose south-west corner is (lat, lon)."""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    name = (f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_"
            f"{ew}{abs(lon):03d}_00_DEM")
    return f"{_COP_S3}/{name}/{name}.tif"


def fetch_dem(scenario_key: str, force: bool = False) -> tuple[Path, Provenance]:
    """
    Fetch Copernicus GLO-30 for the scenario bbox and reproject to local UTM.

    Returns ``(path, provenance)``. Raises on network failure — the caller
    decides whether to fall back to synthetic terrain, so the fallback is
    always a deliberate choice rather than a silent one.
    """
    sc = SCENARIOS[scenario_key]
    w, s, e, n = sc["bbox"]
    out_dir = DATA_DIR / "dem"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{scenario_key}_dem.tif"

    if out_path.exists() and not force:
        _validate_cached_dem(out_path, sc)
        logger.info("DEM already present: %s", out_path)
        return out_path, Provenance.COMPUTED_LIVE

    # Every 1-degree tile the bbox touches
    tiles = [
        _copernicus_tile_url(lat, lon)
        for lat in range(int(np.floor(s)), int(np.floor(n)) + 1)
        for lon in range(int(np.floor(w)), int(np.floor(e)) + 1)
    ]
    logger.info("Fetching Copernicus GLO-30: %d tile(s) for %s", len(tiles), sc["name"])

    srcs = []
    try:
        for url in tiles:
            logger.info("  %s", url.rsplit("/", 1)[-1])
            srcs.append(rasterio.open(url))
        mosaic, mosaic_tr = rio_merge(srcs, bounds=(w, s, e, n))
        src_crs = srcs[0].crs
    finally:
        for ds in srcs:
            ds.close()

    band = mosaic[0]
    logger.info("  mosaicked %s, elevation %.0f–%.0f m",
                band.shape, float(np.nanmin(band)), float(np.nanmax(band)))

    # Reproject to local UTM so dx/dy are true metres and square. The solver
    # assumes a metric grid; feeding it degrees is a silent unit error.
    from rasterio.warp import calculate_default_transform, reproject, Resampling
    dst_crs = rasterio.crs.CRS.from_epsg(sc["utm_epsg"])
    dst_tr, dst_w, dst_h = calculate_default_transform(
        src_crs, dst_crs, band.shape[1], band.shape[0], w, s, e, n)

    dest = np.zeros((dst_h, dst_w), dtype=np.float32)
    reproject(
        source=band, destination=dest,
        src_transform=mosaic_tr, src_crs=src_crs,
        dst_transform=dst_tr, dst_crs=dst_crs,
        resampling=Resampling.bilinear, src_nodata=None, dst_nodata=-9999.0,
    )

    with rasterio.open(
        out_path, "w", driver="GTiff", height=dst_h, width=dst_w, count=1,
        dtype="float32", crs=dst_crs, transform=dst_tr,
        nodata=-9999.0, compress="lzw",
    ) as dst:
        dst.write(dest, 1)

    _write_dem_metadata(out_path, sc, tiles, dst_crs, dst_tr, dest)

    px = abs(dst_tr.a)
    logger.info("DEM → %s  (%dx%d, %.1f m cells, EPSG:%d)",
                out_path, dst_w, dst_h, px, sc["utm_epsg"])
    return out_path, Provenance.COMPUTED_LIVE


def _scenario_projected_bounds(sc: dict) -> tuple[float, float, float, float]:
    from pyproj import Transformer
    w, s, e, n = sc["bbox"]
    tr = Transformer.from_crs("EPSG:4326", f"EPSG:{sc['utm_epsg']}", always_xy=True)
    pts = [tr.transform(x, y) for x, y in ((w, s), (w, n), (e, s), (e, n))]
    xs, ys = zip(*pts)
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_stamp_ok(out_path: Path, sc: dict, write: bool = False) -> bool:
    """Is this cached OSM layer the one the CURRENT scenario bbox asks for?

    These caches key on FILENAME ONLY, so widening a scenario's bbox left them
    silently stale: `annamayya_rivers.geojson` was still the 2026-09-05 fetch on
    the pre-widening AOI, and because `build_rivers` returns the cache unless
    `force=True`, every consumer got the old river network without a word. That
    is not cosmetic -- `route_annamayya.py::locate_handoff` picks the handoff
    section off this layer, so a stale layer silently moves a physics boundary.
    The DEM never had this bug because `_validate_cached_dem` checks its bounds;
    the OSM layers had no equivalent. This is it.

    A sidecar `<name>.bbox.json` records the bbox the cache was built for.
    A cache with no sidecar is treated as UNKNOWN, not as current: these files
    predate the stamp, and assuming they match is the failure mode being fixed.
    """
    side = out_path.with_suffix(".bbox.json")
    want = [float(v) for v in sc["bbox"]]
    if write:
        side.write_text(json.dumps({"bbox": want, "scenario_bbox_stamp": 1}), encoding="utf-8")
        return True
    if not side.exists():
        return False
    try:
        got = json.loads(side.read_text(encoding="utf-8")).get("bbox")
    except Exception:                                     # noqa: BLE001
        return False
    return bool(got) and [float(v) for v in got] == want


def _load_labelled(out_path: Path, sc: dict, kind: str, hand_curated: bool = False):
    """Serve a cached OSM layer, LABELLED with whether it matches this bbox.

    Detect-and-label, not detect-and-refetch. The first version of this guard
    refetched on a mismatch, which found a real bug (`annamayya_rivers.geojson`
    was the 2026-09-05 fetch and had never contained the Pincha arm) but made
    every consumer depend on Overpass being reachable. Overpass throttles, and
    the measured cost was the test suite no longer completing -- it hung in
    `test_run_lifecycle.py`, which runs the full pipeline and touches three OSM
    layers for a scenario with no sidecar.

    So: a layer whose bbox cannot be confirmed is returned with
    `attrs["bbox_stale"] = True` and a warning. Callers that merely draw or
    report it carry on. Callers that site a physics boundary or CARVE TERRAIN
    from it -- `route_annamayya.py::locate_handoff` and `_condition_terrain` --
    refuse that label. Refetching is an explicit `force=True`.
    """
    g = gpd.read_file(out_path)
    if _bbox_stamp_ok(out_path, sc):
        return g
    g.attrs["bbox_stale"] = True
    logger.warning(
        "%s layer %s was built for a different (or unrecorded) bbox — serving it "
        "labelled bbox_stale=True for %s.%s", kind.upper(), out_path.name,
        tuple(sc["bbox"]),
        " This layer is HAND-CURATED; re-authoring it is a deliberate act."
        if hand_curated else " Refetch with force=True to refresh it.")
    return g


def _validate_cached_dem(path: Path, sc: dict) -> None:
    """Reject cached rasters whose metric CRS or AOI does not match scenario."""
    with rasterio.open(path) as ds:
        if ds.crs is None or not ds.crs.is_projected:
            raise ValueError(f"cached DEM {path} has no projected metric CRS; regenerate explicitly")
        expected_crs = rasterio.crs.CRS.from_epsg(int(sc["utm_epsg"]))
        if ds.crs != expected_crs:
            raise ValueError(f"cached DEM {path} has CRS {ds.crs}, expected {expected_crs}; regenerate explicitly")
        expected = _scenario_projected_bounds(sc)
        left, bottom, right, top = ds.bounds
        # Reprojection and floating point rounding can leave an edge short by
        # one half-cell. Permit that normal rasterization tolerance only.
        half_x, half_y = abs(float(ds.transform.a)) / 2.0, abs(float(ds.transform.e)) / 2.0
        if (left > expected[0] + half_x or bottom > expected[1] + half_y or
                right < expected[2] - half_x or top < expected[3] - half_y):
            raise ValueError(f"cached DEM {path} does not cover scenario AOI; regenerate explicitly")
        if (not np.isfinite(ds.transform.a) or not np.isfinite(ds.transform.e) or
                abs(ds.transform.a) <= 0 or abs(ds.transform.e) <= 0):
            raise ValueError(f"cached DEM {path} has invalid metric resolution; regenerate explicitly")


def _write_dem_metadata(path: Path, sc: dict, urls: list[str], crs, transform, array: np.ndarray) -> None:
    meta = {
        "schema_version": 1, "path": path.name, "source_urls": urls,
        "source_sha256": hashlib.sha256(array.tobytes()).hexdigest(),
        "bbox": list(sc["bbox"]), "crs": crs.to_string(),
        "resolution_m": [abs(float(transform.a)), abs(float(transform.e))],
        "nodata": -9999.0, "created_at": datetime.now(timezone.utc).isoformat(),
        "classification": "RASTER_DSM",
    }
    path.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def generate_synthetic_dem(scenario_key: str) -> tuple[Path, Provenance]:
    """
    Analytic stand-in terrain, for running the demo with no network.

    This is NOT a survey product and nothing derived from it is operationally
    meaningful. It returns ``SYNTHETIC_TERRAIN`` so that label propagates to
    every downstream number.
    """
    sc = SCENARIOS[scenario_key]
    w, s, e, n = sc["bbox"]
    out_dir = DATA_DIR / "dem"
    out_dir.mkdir(parents=True, exist_ok=True)
    dem_path = out_dir / f"{scenario_key}_dem_synthetic.tif"

    ny, nx = 200, 250
    lon_grid, lat_grid = np.meshgrid(np.linspace(w, e, nx), np.linspace(n, s, ny))
    river_x = (lon_grid - w) / (e - w)
    river_y = (lat_grid - s) / (n - s)

    valley_axis = 0.6 * river_x + 0.4 * (1.0 - river_y)
    base = sc["thalweg_m"] - 350.0 * valley_axis
    dist_to_river = np.abs(lat_grid - (s + (n - s) * (0.3 + 0.4 * river_x)))
    canyon = base + 600.0 * (dist_to_river / (n - s)) ** 1.6
    ridges = 45.0 * np.sin(river_x * 12.0) * np.cos(river_y * 8.0)
    elevation = (canyon + ridges).astype(np.float32)

    with rasterio.open(
        dem_path, "w", driver="GTiff", height=ny, width=nx, count=1,
        dtype="float32", crs="EPSG:4326",
        transform=from_bounds(w, s, e, n, nx, ny), compress="lzw",
    ) as dst:
        dst.write(elevation, 1)

    logger.warning("SYNTHETIC terrain written → %s (analytic, not surveyed)", dem_path)
    return dem_path, Provenance.SYNTHETIC_TERRAIN


def get_dem(scenario_key: str, allow_synthetic: bool = False,
            force: bool = False) -> tuple[Path, Provenance]:
    """Real DEM, falling back to synthetic only when explicitly permitted."""
    try:
        return fetch_dem(scenario_key, force=force)
    except Exception as exc:
        if not allow_synthetic:
            raise RuntimeError(
                f"Could not fetch Copernicus GLO-30 for '{scenario_key}': {exc}. "
                "Re-run with --offline-demo to use synthetic terrain, which is "
                "clearly labelled and not operationally meaningful."
            ) from exc
        logger.warning("DEM fetch failed (%s) — falling back to synthetic", exc)
        return generate_synthetic_dem(scenario_key)


# ──────────────────────────────────────────────────────────────────────────────
# Population — GHS-POP
# ──────────────────────────────────────────────────────────────────────────────

# GHS-POP R2023A, 3 arc-second (~90 m), WGS84, population COUNT per cell.
# Tiles are 10 deg x 10 deg, numbered R<row>_C<col> with row counting south
# from 90 N and col counting east from 180 W. The tile MUST be chosen from the
# scenario bbox: this was hardcoded to R6_C26 (30-40 N, 70-80 E) back when
# phutkal and rishiganga were the only scenarios, and Derna -- added later at
# 22.6 E -- silently clipped against a tile that does not contain it, yielding
# an all-zero population raster and "0 people / not computed" in the ranking.
_GHSL_BASE = ("https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL/"
              "GHS_POP_GLOBE_R2023A/GHS_POP_E2020_GLOBE_R2023A_4326_3ss/V1-0/tiles")
_GHSL_STEM = "GHS_POP_E2020_GLOBE_R2023A_4326_3ss_V1_0"


def _ghsl_tile_name(lon: float, lat: float) -> str:
    """Name of the 10-degree GHS-POP tile containing (lon, lat)."""
    col = int((lon + 180.0) // 10.0) + 1
    row = int((90.0 - lat) // 10.0) + 1
    return f"{_GHSL_STEM}_R{row}_C{col}"


def _ghsl_tile_path(tile: str, force: bool = False) -> Path:
    """Download and unzip one GHS-POP tile, caching the GeoTIFF."""
    import io
    import urllib.request
    import zipfile

    cache = DATA_DIR / "population" / "_cache"
    cache.mkdir(parents=True, exist_ok=True)
    tif = cache / f"{tile}.tif"
    if tif.exists() and not force:
        return tif

    url = f"{_GHSL_BASE}/{tile}.zip"
    logger.info("Downloading GHS-POP tile (one time, 30-170 MB): %s", tile)
    with urllib.request.urlopen(url, timeout=600) as resp:
        blob = resp.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".tif")]
        if not names:
            raise RuntimeError(f"No GeoTIFF inside {url}")
        with zf.open(names[0]) as src, open(tif, "wb") as dst:
            dst.write(src.read())
    logger.info("GHS-POP tile cached → %s", tif)
    return tif


def fetch_population(scenario_key: str, force: bool = False) -> Path:
    """
    Clip GHS-POP to the scenario DEM grid, conserving population counts.

    The raster holds people per cell, so it is resampled with ``sum`` rather
    than an interpolating method: bilinear would smear counts and silently
    change the total. The output shares the DEM's grid exactly, which keeps the
    zonal statistics in M5 a straight masked sum.
    """
    out_dir = DATA_DIR / "population"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{scenario_key}_ghspop.tif"
    if out_path.exists() and not force:
        meta_path = out_path.with_suffix(".json")
        if not meta_path.exists():
            meta_path.write_text(json.dumps({
                "schema_version": 1, "scenario_key": scenario_key,
                "classification": "PROXY",
                "source": "GHS-POP R2023A E2020",
                "coverage": "scenario DEM grid; OSM settlement tags do not establish historical event population",
                "reason": "population product is a present-day proxy, not event census",
            }, indent=2), encoding="utf-8")
        return out_path

    dem_path = DATA_DIR / "dem" / f"{scenario_key}_dem.tif"
    if not dem_path.exists():
        raise RuntimeError("Fetch the DEM before the population grid — the "
                           "population raster is aligned to it.")

    from rasterio.warp import reproject, Resampling

    sc = SCENARIOS[scenario_key]
    tile = _ghsl_tile_name(sc["lon"], sc["lat"])
    src_tif = _ghsl_tile_path(tile, force=force)
    with rasterio.open(dem_path) as dem, rasterio.open(src_tif) as pop:
        dest = np.zeros((dem.height, dem.width), dtype=np.float32)
        reproject(
            source=rasterio.band(pop, 1), destination=dest,
            src_transform=pop.transform, src_crs=pop.crs,
            dst_transform=dem.transform, dst_crs=dem.crs,
            # No dst_nodata: zero population is a real measurement, not absence.
            resampling=Resampling.sum, src_nodata=pop.nodata,
        )
        profile = dem.profile.copy()

    dest = np.clip(np.nan_to_num(dest, nan=0.0), 0.0, None)
    profile.update(dtype="float32", count=1, nodata=None, compress="lzw",
                   tiled=False)
    profile.pop("blockxsize", None)
    profile.pop("blockysize", None)
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(dest, 1)

    out_path.with_suffix(".json").write_text(json.dumps({
        "schema_version": 1, "scenario_key": scenario_key,
        "classification": "PROXY", "source": "GHS-POP R2023A E2020",
        "coverage": "scenario DEM grid",
        "reason": "population product is a present-day proxy, not event census",
        "crs": str(profile.get("crs")), "nodata": None,
    }, indent=2), encoding="utf-8")

    logger.info("GHS-POP → %s  (%.0f people in the AOI)", out_path, float(dest.sum()))
    return out_path


# ──────────────────────────────────────────────────────────────────────────────
# OpenStreetMap layers
# ──────────────────────────────────────────────────────────────────────────────

# Overpass mirrors, tried in order. The main endpoint rate-limits and times out
# under repeated querying, which is exactly what a session that refetches several
# layers does -- measured 2026-09-14, a connect timeout on overpass-api.de while
# the same query succeeded seconds later on a mirror. Failing the whole run on one
# endpoint's throttle is a worse outcome than spending a few seconds on a retry.
# osmnx 2.x names this `settings.overpass_url` and wants the API BASE, with no
# /interpreter. osmnx 1.x called it `overpass_endpoint`. Setting the wrong one
# fails silently -- every "mirror" then queries the default host, which is what
# happened on the first attempt at this fix: three mirrors, three identical
# timeouts against overpass-api.de. The setattr below is therefore checked.
_OVERPASS_MIRRORS = (
    "https://overpass-api.de/api",
    "https://overpass.kumi.systems/api",
    "https://overpass.osm.jp/api",
    "https://overpass.private.coffee/api",
)


def _set_overpass(url: str) -> str:
    """Point osmnx at `url`. Returns the previous value. Raises if neither
    settings name exists -- a silent no-op here is what made the mirrors fake."""
    import osmnx as ox
    for attr in ("overpass_url", "overpass_endpoint"):
        if hasattr(ox.settings, attr):
            prev = getattr(ox.settings, attr)
            setattr(ox.settings, attr, url)
            return prev
    raise RuntimeError("osmnx exposes neither settings.overpass_url nor "
                       "settings.overpass_endpoint; cannot select a mirror")


# Once every mirror has refused, stop paying for it. Four mirrors x 300 s is 20
# minutes PER CALL, and a test suite that touches three OSM layers then spends an
# hour discovering the same outage three times -- which is exactly how the suite
# started hanging at 61 %. Reset it by reimporting or setting it back to False.
_OVERPASS_DOWN = False


def _osm_features(bbox, tags: dict):
    """Query OSM features; distinguish empty result from network failure."""
    global _OVERPASS_DOWN
    import osmnx as ox
    w, s, e, n = bbox
    if _OVERPASS_DOWN:
        raise RuntimeError(
            f"OSM query for tags {tags} skipped: every Overpass mirror already "
            "failed once in this process (circuit breaker). Cached layers are "
            "still served, labelled stale.")
    prev_timeout = getattr(ox.settings, "requests_timeout", None)
    # A read timeout means the mirror ACCEPTED the query and is computing it --
    # a full-bbox waterway query is genuinely slow, and 60 s killed mirrors that
    # were working. But 300 s x 4 mirrors is 20 minutes of wall time per call, and
    # the caller that pays it is usually a test or a diagnostic that would have
    # been perfectly happy with the cached layer. 120 s is long enough for a real
    # query and caps the outage penalty at 8 minutes, once, before the circuit
    # breaker below makes every later call instant.
    ox.settings.requests_timeout = 120
    prev_url = None
    last = None
    try:
        for url in _OVERPASS_MIRRORS:
            got = _set_overpass(url)
            if prev_url is None:
                prev_url = got
            try:
                result = ox.features_from_bbox(bbox=(w, s, e, n), tags=tags)
                if result is None:
                    return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
                if url != _OVERPASS_MIRRORS[0]:
                    logger.warning("OSM: primary Overpass endpoint failed; served by %s", url)
                return result
            except Exception as exc:                       # noqa: BLE001
                last = exc
                logger.warning("OSM: %s failed (%s) — trying the next mirror",
                               url, str(exc)[:140])
    finally:
        if prev_url is not None:
            _set_overpass(prev_url)
        if prev_timeout is not None:
            ox.settings.requests_timeout = prev_timeout
    _OVERPASS_DOWN = True
    raise RuntimeError(f"OSM query failed for tags {tags} on all "
                       f"{len(_OVERPASS_MIRRORS)} mirrors: {last}") from last


def _is_legacy_synthetic_graph(graph) -> bool:
    if len(graph.nodes) != 8 or len(graph.edges) != 14:
        return False
    for _, _, data in graph.edges(data=True):
        if float(data.get("length", -1)) != 2500.0 or data.get("highway") != "primary":
            return False
    return True


def download_osm_road_network(scenario_key: str, force: bool = False) -> Path:
    """Download and cache the drivable road network."""
    import osmnx as ox
    sc = SCENARIOS[scenario_key]
    w, s, e, n = sc["bbox"]
    out_dir = DATA_DIR / "roads"
    out_dir.mkdir(parents=True, exist_ok=True)
    graph_path = out_dir / f"{scenario_key}_roads.graphml"

    if graph_path.exists() and not force:
        import networkx as nx
        cached = nx.read_graphml(graph_path)
        if _is_legacy_synthetic_graph(cached):
            raise RuntimeError(f"cached road graph {graph_path} matches rejected synthetic 8-node/14-edge signature")
        return graph_path

    logger.info("Fetching OSM road network for %s …", sc["name"])
    try:
        G = ox.graph_from_bbox(bbox=(w, s, e, n), network_type="drive", retain_all=True)
    except Exception as exc:
        raise RuntimeError(f"OSM road network fetch failed for '{scenario_key}': {exc}") from exc
    if _is_legacy_synthetic_graph(G):
        raise RuntimeError("OSM returned graph matching rejected synthetic 8-node/14-edge signature")
    ox.save_graphml(G, filepath=graph_path)
    logger.info("Road graph → %s (%d nodes, %d edges)",
                graph_path, len(G.nodes), len(G.edges))
    return graph_path


def build_villages(scenario_key: str, force: bool = False) -> gpd.GeoDataFrame:
    """
    Build village polygons from real OSM ``place`` nodes.

    OSM rarely carries population for Himalayan settlements. Where the tag is
    absent ``pop_total`` is 0 and M5 reports population as NOT computable rather
    than inventing a figure — the honest state until GHS-POP is wired.
    """
    sc = SCENARIOS[scenario_key]
    out_dir = DATA_DIR / "admin"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{scenario_key}_villages.geojson"

    # NO bbox invalidation here, deliberately, unlike rivers/facilities/buildings.
    # This file IS the population layer (INVARIANTS S1) and it is HAND-CURATED:
    # annamayya_villages.geojson carries a corrected Nandalur coordinate, a
    # reconciled population, and provenance fields on features. Silently
    # refetching it on a bbox change would delete all of that. It warns instead.
    if out_path.exists() and not force:
        return _load_labelled(out_path, sc, "villages", hand_curated=True)

    places = _osm_features(sc["bbox"], {"place": ["village", "hamlet", "town", "city"]})
    rows = []
    if len(places):
        places = places[places.geometry.notna()]
        for i, (_, p) in enumerate(places.iterrows()):
            name = p.get("name")
            if not name or not isinstance(name, str):
                continue
            pt = p.geometry.centroid
            pop = p.get("population")
            try:
                pop = int(str(pop).replace(",", "")) if pop is not None else 0
            except (TypeError, ValueError):
                pop = 0
            rows.append({
                "village_id":   f"V{i + 1:03d}",
                "village_name": name,
                "place_type":   p.get("place", "village"),
                "pop_total":    pop,
                "lon":          float(pt.x),
                "lat":          float(pt.y),
                # A settlement footprint proxy; OSM place nodes have no extent.
                "geometry":     Point(pt.x, pt.y).buffer(0.008),
            })

    if not rows:
        raise RuntimeError(
            f"No OSM settlements found for '{scenario_key}'. The AOI may be "
            "genuinely unmapped — pick another demo AOI rather than inventing "
            "villages."
        )

    gdf = gpd.GeoDataFrame(rows, crs="EPSG:4326")
    gdf.to_file(out_path, driver="GeoJSON")
    _bbox_stamp_ok(out_path, sc, write=True)
    n_pop = int((gdf["pop_total"] > 0).sum())
    logger.info("Villages → %s (%d settlements, %d with an OSM population tag)",
                out_path, len(gdf), n_pop)
    return gdf


def build_rivers(scenario_key: str, force: bool = False) -> gpd.GeoDataFrame:
    """
    River and stream centrelines from OSM.

    A landslide dam forms in a river, so the breach has to sit on one. Snapping
    to "the lowest cell within a radius" instead finds whatever local minimum is
    nearest — on the Phutkal tile that was a plateau at 4,586 m, roughly 800 m
    above the actual gorge, which then "impounded" 93 km³ of water.
    """
    sc = SCENARIOS[scenario_key]
    out_dir = DATA_DIR / "admin"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{scenario_key}_rivers.geojson"

    if out_path.exists() and not force:
        return _load_labelled(out_path, sc, "rivers")

    rivers = _osm_features(sc["bbox"],
                           {"waterway": ["river", "stream", "riverbank"]})
    if len(rivers):
        rivers = rivers[rivers.geometry.notna()]
        rivers = rivers[rivers.geometry.geom_type.isin(["LineString", "MultiLineString"])]
        gdf = gpd.GeoDataFrame(
            {"name": rivers.get("name", "").astype(str),
             "waterway": rivers.get("waterway", "").astype(str)},
            geometry=rivers.geometry.values, crs="EPSG:4326")
    else:
        gdf = gpd.GeoDataFrame({"name": [], "waterway": []},
                               geometry=[], crs="EPSG:4326")

    gdf.to_file(out_path, driver="GeoJSON")
    _bbox_stamp_ok(out_path, sc, write=True)
    logger.info("Rivers → %s (%d segments)", out_path, len(gdf))
    return gdf


def build_facilities(scenario_key: str, force: bool = False) -> gpd.GeoDataFrame:
    """Hospitals, clinics and schools from OSM. Often empty in the high Himalaya."""
    sc = SCENARIOS[scenario_key]
    out_dir = DATA_DIR / "admin"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{scenario_key}_facilities.geojson"

    if out_path.exists() and not force:
        return _load_labelled(out_path, sc, "facilities")

    facs = _osm_features(sc["bbox"],
                         {"amenity": ["hospital", "clinic", "doctors", "school"]})
    if not len(facs):
        logger.warning("No OSM facilities in %s — reported as 0, not estimated",
                       sc["name"])
        gdf = gpd.GeoDataFrame({"amenity": [], "name": []},
                               geometry=[], crs="EPSG:4326")
    else:
        facs = facs[facs.geometry.notna()].copy()
        # Normalise clinics and doctors to "hospital" for the exposure counter.
        facs["amenity"] = facs["amenity"].replace(
            {"clinic": "hospital", "doctors": "hospital"})
        gdf = gpd.GeoDataFrame(
            {"amenity": facs["amenity"].astype(str),
             "name": facs.get("name", "").astype(str)},
            geometry=facs.geometry.values, crs="EPSG:4326")

    gdf.to_file(out_path, driver="GeoJSON")
    _bbox_stamp_ok(out_path, sc, write=True)
    logger.info("Facilities → %s (%d)", out_path, len(gdf))
    return gdf


def build_buildings(scenario_key: str, force: bool = False) -> gpd.GeoDataFrame:
    """OSM building footprints. Sparse here — that sparsity is reported, not filled."""
    sc = SCENARIOS[scenario_key]
    out_dir = DATA_DIR / "buildings"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{scenario_key}_buildings.geojson"

    if out_path.exists() and not force:
        return _load_labelled(out_path, sc, "buildings")

    b = _osm_features(sc["bbox"], {"building": True})
    if len(b):
        b = b[b.geometry.notna()]
        gdf = gpd.GeoDataFrame(geometry=b.geometry.values, crs="EPSG:4326")
    else:
        gdf = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    gdf.to_file(out_path, driver="GeoJSON")
    _bbox_stamp_ok(out_path, sc, write=True)
    logger.info("Buildings → %s (%d footprints)", out_path, len(gdf))
    return gdf


def prepare_all(scenario_key: str | None = None, force: bool = False) -> None:
    """Fetch every layer for one scenario, or for all of them."""
    keys = [scenario_key] if scenario_key else list(SCENARIOS)
    for k in keys:
        logger.info("=" * 60)
        logger.info("Preparing %s", SCENARIOS[k]["name"])
        logger.info("=" * 60)
        get_dem(k, force=force)
        fetch_population(k, force=force)
        download_osm_road_network(k, force=force)
        build_villages(k, force=force)
        build_facilities(k, force=force)
        build_buildings(k, force=force)


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    ap = argparse.ArgumentParser(description="Fetch FloodSight datasets")
    ap.add_argument("--scenario", choices=list(SCENARIOS), default=None)
    ap.add_argument("--force", action="store_true", help="re-download even if cached")
    args = ap.parse_args()
    prepare_all(args.scenario, force=args.force)
