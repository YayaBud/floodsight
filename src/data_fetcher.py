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
        "name": "Phutkal River Landslide Dam (2015)",
        "lat": 33.25242, "lon": 77.05783,
        "bbox": (76.75, 33.12, 77.10, 33.40),      # west, south, east, north
        "utm_epsg": 32643,                          # UTM 43N
        "wse_m": 3878.0,
        "thalweg_m": 3820.0,
        "breach_lon": 77.05783, "breach_lat": 33.25242,
        "dam_height_m": 58.0,
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
    "derna": {
        "name": "Derna Dams — Abu Mansour + Al-Bilad, Libya (Sep 2023)",
        "lat": 32.65755, "lon": 22.57733,
        "bbox": (22.50, 32.60, 22.72, 32.82),
        "utm_epsg": 32634,                          # UTM 34N
        "wse_m": 170.0,
        "thalweg_m": 96.0,
        "breach_lon": 22.57733, "breach_lat": 32.65755,
        "dam_height_m": 74.0,
        "volume_mcm": 22.5,
        "event_type": "cascading_dam_break",
        "upstream_structure": {
            "name": "Abu Mansour Dam (Upstream)",
            "lat": 32.65755, "lon": 22.57733,
            "height_m": 74.0, "volume_mcm": 22.5,
            "status": "Overtopped and burst at 02:30 AM",
            "role": "Primary upstream breach",
        },
        "downstream_structure": {
            "name": "Al-Bilad Dam (Downstream City Dam)",
            "lat": 32.75237, "lon": 22.63058,
            "height_m": 45.0, "volume_mcm": 1.5,
            "distance_km": 13.7,
            "status": "Overtopped and destroyed at 03:00 AM (T+30 min)",
            "role": "Secondary cascading failure into Derna city",
        },
        "cascade_arrival_min": 30.0,
        "failure_trigger": "Storm Daniel precipitation + upstream reservoir overtopping",
        # ── Generic cascade routing config ────────────────────────────────────
        # CLASSIFICATION LEGEND:
        #   upstream.peak_q_m3s: RECONSTRUCTION from 22.5 MCM / ~30min formation
        #   routing: ASSUMPTION (flash-flood steep wadi, shorter K than natural river)
        #   reservoir: OFFICIAL ESTIMATE from UNEP / Copernicus post-event survey
        #   breach_ensemble: MODEL RECONSTRUCTION (Froehlich 2008)
        "cascade": {
            "scenario_key": "derna",
            "upstream": {
                "peak_q_m3s":   8000.0,     # RECONSTRUCTION: Abu Mansour 22.5 MCM rapid release
                "base_q_m3s":    100.0,     # wadi base flow during Storm Daniel
                "duration_s":   3600.0,     # ~60 min surge (steep wadi, fast emptying)
                "lead_time_s":  1800.0,     # 30 min before downstream breach
                "classification": "RECONSTRUCTION",
            },
            "routing": {
                "k_s":          1200.0,     # ASSUMPTION: 13.7 km steep wadi ~K=20 min
                "x":               0.15,
                "classification": "ASSUMPTION",
            },
            "reservoir": {
                "z_bed_m":        96.0,     # OFFICIAL ESTIMATE: Al-Bilad thalweg
                "z_frl_m":       135.0,     # ASSUMPTION: estimated FRL from dam height
                "z_crest_m":     141.0,     # OFFICIAL ESTIMATE: Al-Bilad crest
                "v_frl_mcm":       1.5,     # OFFICIAL ESTIMATE: Al-Bilad capacity
                "alpha_exp":       2.5,
                "classification": "OFFICIAL ESTIMATE",
            },
            "spillway": {
                "cd":              2.00,
                "length_m":       30.0,
                "z_crest_m":     138.0,
                "max_q_m3s":    1200.0,     # ASSUMPTION: small spillway capacity
            },
            "catchment_runoff": {
                "base_m3s":       50.0,
                "peak_m3s":     3500.0,     # RECONSTRUCTION: Storm Daniel wadi catchment
                "peak_offset_s": -900.0,
                "sigma_s":      3600.0,
            },
            "breach_ensemble": {
                "optimistic":  {"width_m": 20.0, "formation_s": 1200.0, "peak_q_m3s":  5000.0,
                                "classification": "MODEL RECONSTRUCTION",
                                "source": "Froehlich (2008) lower CI — small embankment dam"},
                "central":     {"width_m": 35.0, "formation_s":  900.0, "peak_q_m3s":  9500.0,
                                "classification": "MODEL RECONSTRUCTION",
                                "source": "Froehlich (2008) best estimate — Al-Bilad 1.5 MCM"},
                "pessimistic": {"width_m": 55.0, "formation_s":  600.0, "peak_q_m3s": 16000.0,
                                "classification": "MODEL RECONSTRUCTION",
                                "source": "Froehlich (2008) upper — UNEP estimated >15,000 m3/s surge"},
            },
        },
    },
    "malpasset": {
        "name": "Malpasset Arch Dam — France (1959, Canonical Benchmark)",
        "lat": 43.51216, "lon": 6.75684,
        "bbox": (6.65, 43.41, 6.82, 43.55),
        "utm_epsg": 32632,                          # UTM 32N
        "wse_m": 101.5,
        "thalweg_m": 35.0,
        "breach_lon": 6.75684, "breach_lat": 43.51216,
        "dam_height_m": 66.5,
        "volume_mcm": 50.0,
        "downstream_structure": {
            "name": "Bozon Highway Bridge & Frejus Estuary",
            "lat": 43.4350, "lon": 6.7420,
            "distance_km": 8.0,
            "status": "Bridge destroyed; Frejus submerged in 20 min",
            "role": "Downstream coastal inundation",
        },
    },
    "ivanovo": {
        "name": "Ivanovo Dam — Bulgaria (2012, GFD Observed)",
        "lat": 41.86243, "lon": 25.85388,
        "bbox": (25.75, 41.78, 26.05, 41.96),
        "utm_epsg": 32635,                          # UTM 35N
        "wse_m": 171.0,
        "thalweg_m": 155.0,
        "breach_lon": 25.85388, "breach_lat": 41.86243,
        "dam_height_m": 16.0,
        "volume_mcm": 2.5,
        "downstream_structure": {
            "name": "Biser Village Bridge & Dykes",
            "lat": 41.8833, "lon": 25.8833,
            "distance_km": 3.8,
            "status": "Submerged within 20 min of breach",
            "role": "Downstream village inundation",
        },
    },
    "south_lhonak": {
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
        "name": "Annamayya Dam Failure & Pincha Cascade — Andhra Pradesh (Nov 2021)",
        "lat": 14.21059, "lon": 79.02128,
        "bbox": (78.96, 14.12, 79.28, 14.36),
        "utm_epsg": 32644,                          # UTM 44N
        "wse_m": 206.0,                             # Overtopping crest elevation (+206.000 m MSL, FRL is +203.600 m MSL)
        "thalweg_m": 180.0,                         # Deepest bed / breach invert (+180.000 m MSL)
        "breach_lon": 79.02128, "breach_lat": 14.21059,
        "dam_height_m": 26.0,                       # Crest - Thalweg (206.0 - 180.0 m)
        "volume_mcm": 63.43,                        # Gross storage at FRL (81.2 MCM at crest overtopping)
        "event_type": "cascading_dam_break",
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
            "name": "Pincha Dam (Upstream)",
            "lat": 13.90890, "lon": 78.99956,
            "height_m": 15.0, "volume_mcm": 14.0,
            "status": "Overtopped & ring bund breached at 03:30 AM (T-150 min)",
            "role": "Upstream feeder breach triggering Cheyyeru surge",
        },
        "downstream_structure": {
            "name": "Annamayya Dam (Downstream)",
            "lat": 14.21059, "lon": 79.02128,
            "height_m": 26.0, "volume_mcm": 63.43,
            "distance_km": 34.0,
            "status": "Overtopped & earthen bund washed out at 06:00 AM (T=0)",
            "role": "Downstream catastrophic failure",
        },
        "cascade_arrival_min": 150.0,
        "failure_trigger": "Upstream Pincha breach surge + jammed spillway gate #4",
    },
}


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

    px = abs(dst_tr.a)
    logger.info("DEM → %s  (%dx%d, %.1f m cells, EPSG:%d)",
                out_path, dst_w, dst_h, px, sc["utm_epsg"])
    return out_path, Provenance.COMPUTED_LIVE


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

    logger.info("GHS-POP → %s  (%.0f people in the AOI)", out_path, float(dest.sum()))
    return out_path


# ──────────────────────────────────────────────────────────────────────────────
# OpenStreetMap layers
# ──────────────────────────────────────────────────────────────────────────────

def _osm_features(bbox, tags: dict):
    """Query OSM features for a bbox, returning an empty frame on any failure."""
    import osmnx as ox
    w, s, e, n = bbox
    try:
        return ox.features_from_bbox(bbox=(w, s, e, n), tags=tags)
    except Exception as exc:
        logger.warning("OSM query %s returned nothing (%s)", tags, exc)
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")


def download_osm_road_network(scenario_key: str, force: bool = False) -> Path:
    """Download and cache the drivable road network."""
    import osmnx as ox
    sc = SCENARIOS[scenario_key]
    w, s, e, n = sc["bbox"]
    out_dir = DATA_DIR / "roads"
    out_dir.mkdir(parents=True, exist_ok=True)
    graph_path = out_dir / f"{scenario_key}_roads.graphml"

    if graph_path.exists() and not force:
        return graph_path

    logger.info("Fetching OSM road network for %s …", sc["name"])
    try:
        G = ox.graph_from_bbox(bbox=(w, s, e, n), network_type="drive", retain_all=True)
    except Exception as exc:
        logger.warning("OSM road network fetch failed (%s); building connected valley corridor graph", exc)
        import networkx as nx
        G = nx.MultiDiGraph(crs="EPSG:4326")
        step_x = (e - w) / 8
        step_y = (n - s) / 8
        for i in range(8):
            x = w + i * step_x + 0.01
            y = s + i * step_y + 0.01
            G.add_node(i, x=x, y=y)
            if i > 0:
                G.add_edge(i - 1, i, length=2500.0, highway="primary", bridge=False)
                G.add_edge(i, i - 1, length=2500.0, highway="primary", bridge=False)
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

    if out_path.exists() and not force:
        return gpd.read_file(out_path)

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
        return gpd.read_file(out_path)

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
    logger.info("Rivers → %s (%d segments)", out_path, len(gdf))
    return gdf


def build_facilities(scenario_key: str, force: bool = False) -> gpd.GeoDataFrame:
    """Hospitals, clinics and schools from OSM. Often empty in the high Himalaya."""
    sc = SCENARIOS[scenario_key]
    out_dir = DATA_DIR / "admin"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{scenario_key}_facilities.geojson"

    if out_path.exists() and not force:
        return gpd.read_file(out_path)

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
    logger.info("Facilities → %s (%d)", out_path, len(gdf))
    return gdf


def build_buildings(scenario_key: str, force: bool = False) -> gpd.GeoDataFrame:
    """OSM building footprints. Sparse here — that sparsity is reported, not filled."""
    sc = SCENARIOS[scenario_key]
    out_dir = DATA_DIR / "buildings"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{scenario_key}_buildings.geojson"

    if out_path.exists() and not force:
        return gpd.read_file(out_path)

    b = _osm_features(sc["bbox"], {"building": True})
    if len(b):
        b = b[b.geometry.notna()]
        gdf = gpd.GeoDataFrame(geometry=b.geometry.values, crs="EPSG:4326")
    else:
        gdf = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    gdf.to_file(out_path, driver="GeoJSON")
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
