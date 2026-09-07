# FloodSight — SIH26161

> **"NDSA has a plan for all 6,628 specified dams. Nobody has a plan for the dam that formed last Tuesday. We build that in twenty minutes from open data."**

A rapid consequence-assessment engine for **unmapped impoundments** — landslide dams, moraine-dammed lakes, and blocked river reaches — sponsored by NTRO, Smart India Hackathon 2026.

**Theme:** Disaster Management · **Sponsor:** NTRO · **Deadline:** 20 Sep 2026

---

## Table of Contents

- [What Makes This Different](#what-makes-this-different)
- [Preconfigured Scenarios](#preconfigured-scenarios)
- [Architecture](#architecture)
- [Module Deep Dive](#module-deep-dive)
  - [M1 — Hazard Ingest](#m1--hazard-ingest)
  - [M2 — Geometry & Stage-Storage](#m2--geometry--stage-storage)
  - [M3 — Breach Ensemble](#m3--breach-ensemble)
  - [M4 — 2D Shallow-Water Solver](#m4--2d-shallow-water-solver)
  - [M5 — Population & Infrastructure Exposure](#m5--population--infrastructure-exposure)
  - [M6 — Time-Varying Road Isolation ★](#m6--time-varying-road-isolation-)
  - [M7 — Priority Ranking](#m7--priority-ranking)
  - [M8 — Output Exporters](#m8--output-exporters)
  - [M9 — Dashboard](#m9--dashboard)
  - [M10 — Validation](#m10--validation)
- [Provenance System](#provenance-system)
- [Data Sources](#data-sources)
- [Quick Start](#quick-start)
- [CLI Reference](#cli-reference)
- [API Reference](#api-reference)
- [Frontend](#frontend)
- [Test Suite](#test-suite)
- [Solver Validation Results](#solver-validation-results)
- [Repository Structure](#repository-structure)
- [Dependencies](#dependencies)
- [Known Limitations & Honest Gaps](#known-limitations--honest-gaps)
- [PS Deliverable Compliance](#ps-deliverable-compliance)
- [License & Attribution](#license--attribution)

---

## What Makes This Different

| Capability | RBSD (C-DAC/NDSA) | C-FLOOD (CWC) | **FloodSight** |
|---|---|---|---|
| Coverage | 6,628 specified dams | 3 river basins | **Any impoundment, including unengineered** |
| Breach ensemble | Single deterministic | N/A | **Froehlich + Von Thun + MacDonald → confidence bands** |
| Cascading failures | ❌ | ❌ | **✅ Muskingum routing → downstream reservoir → breach** |
| Time-varying road isolation | ❌ | ❌ | **✅ graph cut per timestep against the depth raster** |
| Satellite integration | Limited | Limited | **Sentinel-1 SAR + Sentinel-2 NDWI via GEE** |
| Validation | Not published | Not published | **CSI/POD/FAR/Bias against Copernicus EMS + GFD** |
| Access | Login-only | Public | **Analyst-facing, open data** |

RBSD already does dam-break inundation with Population at Risk and Infrastructure at Risk. We do not claim that as novel. What it does not do is *ad-hoc impoundments that have no EAP and no precomputed map*, and it does not cut a road graph over time.

---

## Preconfigured Scenarios

FloodSight ships with **7 preconfigured scenarios** spanning 4 countries, multiple dam types, and cascading failure modes:

| Scenario | Event | Country | Dam Height | Volume | Key Feature |
|---|---|---|---|---|---|
| `phutkal` | Phutkal River Landslide Dam (2015) | India 🇮🇳 | 58 m | 30 MCM | 15 km canyon impoundment, Zanskar gorge |
| `rishiganga` | Rishi Ganga Avalanche & Cascade (Feb 2021) | India 🇮🇳 | 70 m | 15 MCM | Rock-ice avalanche → HEP cascade → Tapovan NTPC |
| `south_lhonak` | South Lhonak GLOF & Chungthang Dam (Oct 2023) | India 🇮🇳 | 60 m | 50 MCM | GLOF → 42 km cascade → Teesta III HEP washed away |
| `annamayya` | Annamayya Dam Failure & Pincha Cascade (Nov 2021) | India 🇮🇳 | 26 m | 63.4 MCM | Pincha breach → 34 km surge → jammed spillway gate |
| `derna` | Derna Dams — Abu Mansour + Al-Bilad (Sep 2023) | Libya 🇱🇾 | 74 m | 22.5 MCM | Storm Daniel → cascading dam-break into city ★ **validation case** |
| `malpasset` | Malpasset Arch Dam (1959) | France 🇫🇷 | 66.5 m | 50 MCM | Canonical engineering benchmark |
| `ivanovo` | Ivanovo Dam (2012) | Bulgaria 🇧🇬 | 16 m | 2.5 MCM | GFD-observed dam break |

Each scenario carries:
- Precise WGS84 breach coordinates and UTM zone
- Upstream/downstream structure metadata (name, location, failure status, timeline)
- Lake formation parameters (trigger mechanism, formation time, impounded length)
- Full cascade routing configuration (Muskingum K/x, reservoir stage-storage, spillway capacity, catchment runoff)
- Three-arm breach ensemble (optimistic/central/pessimistic) with Froehlich 2008 citations
- Epistemic classification labels on every parameter: `OBSERVED`, `OFFICIAL ESTIMATE`, `RECONSTRUCTION`, `ASSUMPTION`

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        FloodSight Pipeline                         │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  M1 Hazard Ingest    → National Register + analyst draw + GEE SAR  │
│         ↓                                                           │
│  M2 Geometry         → Blockage on the river, seeded fill, V(h)   │
│         ↓               stage-storage from DEM                     │
│  M3 Breach Ensemble  → Froehlich (2008) + Von Thun (1990)         │
│         ↓               + MacDonald (1984) → 3-arm Q(t)           │
│  M4 Solver           → Well-balanced 2D SWE, MUSCL + SSP-RK2     │
│         ↓               Active window, Ritter benchmark            │
│  M5 Exposure         → Village polygons × depth raster            │
│         ↓               GHS-POP zonal sums, JRC damage curves     │
│  M6 Isolation ★      → OSM graph, per-timestep edge cuts          │
│         ↓               Component reachability → evacuation window │
│  M7 Priority Ranking → Isolation time + PAR + egress + facilities │
│         ↓                                                           │
│  M8 Outputs          → .shp / .kml / CAP JSON / GeoTIFF           │
│         ↓                                                           │
│  M9 Dashboard        → MapLibre GL JS + Plotly + time scrubber    │
│                                                                     │
│  M10 Validation      → CSI / POD / FAR / Bias against observed    │
│                         Copernicus EMS, Global Flood Database      │
└─────────────────────────────────────────────────────────────────────┘
```

The pipeline runs through `run_pipeline.py` which chains M1→M8 in sequence. Each module writes intermediate outputs to `data/scenarios/<job_id>/`. The FastAPI server (`src/api/main.py`) wraps this as an async job with progress polling.

---

## Module Deep Dive

### M1 — Hazard Ingest

**File:** `src/m1_ingest/national_register.py`

Loads scenario configuration from the `SCENARIOS` dictionary in `src/data_fetcher.py`. Each scenario specifies:
- Geographic coordinates and bounding box
- Dam/blockage geometry (height, volume, WSE, thalweg elevation)
- Event type classification (`natural_lake_formation`, `cascading_dam_break`, etc.)
- Upstream/downstream structure descriptions with failure timelines

The data fetcher (`src/data_fetcher.py`) handles all geospatial data acquisition:
- **DEM**: Copernicus GLO-30 tiles fetched from public AWS S3 as windowed COGs, mosaicked and reprojected to local UTM
- **Roads**: OpenStreetMap via OSMnx, cached as GraphML
- **Villages**: OSM `place` nodes — real settlement names and positions
- **Buildings**: OSM building footprints
- **Facilities**: OSM `amenity` = hospital / clinic / school
- **Population**: GHS-POP 3 arc-second tiles, per-scenario bbox

### M2 — Geometry & Stage-Storage

**File:** `src/m2_geometry/fill.py`, `src/m2_geometry/dem_utils.py`

Given a conditioned DEM (UTM, Copernicus GLO-30 or FABDEM bare-earth):
1. Finds the impoundment as the connected water body from a seed point using `scipy.ndimage.label`
2. Fills the DEM to the water surface elevation (WSE) to estimate impounded volume
3. Builds the **stage-storage curve** `V(h)` and **area-elevation curve** `A(h)`
4. Estimates dam crest elevation and freeboard

Returns an `ImpoundmentGeometry` dataclass with:
- `volume_m3`, `area_m2`, `freeboard_m`, `wse_m`, `crest_elev_m`, `dam_height_m`
- Stage curves: `stage_curve_h`, `stage_curve_V`, `stage_curve_A`

**Note:** 30 m DEM gives ~10–20% volume uncertainty in gorges. This is stated, not hidden.

If the fill reaches the domain edge (unconfined pool), it falls back to the configured volume and labels it `PROXY_DATA`.

### M3 — Breach Ensemble

**Files:** `src/m3_breach/froehlich.py`, `src/m3_breach/von_thun.py`, `src/m3_breach/macdonald.py`, `src/m3_breach/ensemble.py`, `src/m3_breach/cascade.py`

Three independent breach parameter methods:

| Method | What it gives | Source |
|---|---|---|
| **Froehlich (2008)** | `B_avg`, `t_f`, `Q_p` — CWC-recommended for Indian dams | Froehlich, Table 2 & Eqs 8–10 |
| **Von Thun & Gillette (1990)** | `B_avg`, `t_f`, `Q_p` (via SCS breach geometry) | USBR internal document |
| **MacDonald & Langemeier (1984)** | `Q_p`, `V_eroded` → `B_avg` via eroded prism | ASCE JHE 110(5) |

**Ensemble logic:**
- **Central arm**: Froehlich (CWC-recommended)
- **Pessimistic arm**: highest `Q_p` among the three methods
- **Optimistic arm**: lowest `Q_p` among the three methods

**Hydrograph shape:** Simplified triangular (USBR SCS-style) — rises linearly to `Q_p` over `0.5 × t_f`, falls linearly to `0.1 × Q_p` over `1.5 × t_f`.

**Engineering calibration:** If the scenario carries a known crest length or dam type (concrete/masonry), the ensemble caps breach width at the structural limit and applies concrete-specific formation time floors.

**Cascade routing** (`cascade.py`): For multi-structure scenarios (Derna, Rishiganga, South Lhonak, Annamayya), the module implements:
- **Muskingum routing** from upstream breach to downstream reservoir
- **Level-pool reservoir routing** with spillway capacity
- **Catchment runoff** superposition (Gaussian pulse)
- **Downstream breach hydrograph** generation per ensemble arm

Each cascade parameter carries an epistemic classification: `OBSERVED`, `OFFICIAL ESTIMATE`, `RECONSTRUCTION`, or `ASSUMPTION`.

### M4 — 2D Shallow-Water Solver

**Files:** `src/m4_solvers/swe_2d.py`, `src/m4_solvers/swe_2d_gpu.py`, `src/m4_solvers/ritter.py`, `src/m4_solvers/validation.py`, `src/m4_solvers/roughness.py`

A from-scratch 2D Saint-Venant / shallow water solver, second order in space and time.

#### Numerical Scheme

| Component | Method |
|---|---|
| **Well-balancing** | Audusse hydrostatic reconstruction — bed-slope source is built into interface fluxes |
| **Spatial reconstruction** | MUSCL with minmod limiter — 2nd order in space |
| **Time integration** | SSP-RK2 (Heun) — 2nd order, strong-stability-preserving |
| **Riemann solver** | Rusanov (local Lax-Friedrichs) — robust across shocks and dry fronts |
| **Friction** | Semi-implicit (point-implicit) — unconditionally stable in thin films |
| **Desingularisation** | Kurganov-Petrova — velocity recovery without blow-up at zero depth |
| **Mass accounting** | Injected, stored, boundary outflow and positivity correction all tracked |

#### Active Window Optimisation

Measured on Derna runs: at peak flood **1.1% of cells are wet** and their bounding box is **6.4% of the domain**. The solver restricts each timestep to a bounding box around wet cells plus a 6-cell halo.

This is an **optimisation, not an approximation** — a dry cell contributes *exactly* zero (with `hL = hR = 0` the Rusanov flux vanishes), so the windowed update reproduces the full-domain result **bit for bit**. The invariant is enforced in `tests/test_swe_active_window.py` with `==` (not a tolerance).

#### Why this replaced the previous solver

The earlier scheme discretised the bed-slope source as `-g*h*dz/dx` by central differences while the flux carried `g*h²/2`. Those do not cancel. Measured on real Phutkal terrain:

| Benchmark | Before | After |
|---|---|---|
| Lake at rest, spurious velocity | **20.0 m/s** (saturating a hard clamp that *hid* it) | **0.0 m/s** |
| Lake at rest, free-surface drift (10 min) | **39 m** | **0.0 m** |
| Mass closure, conical bowl | **31.5% error** | **< 1%** |
| Ritter RMSE | 0.145 m | **0.043 m** |
| Ritter wave-front error | 26% | **10%** |

All five asserted in `tests/test_swe_validation.py`.

#### Roughness

Manning's `n` comes from:
- **Height above thalweg** — channel vs floodplain distinction
- **Urban mask** from GHS-POP population grid (where people live = built up)
- **ESA WorldCover LULC** — 11 land cover classes mapped to Manning coefficients (when available)

The population-based proxy is labelled `PROXY_DATA`. Real ESA WorldCover classification (`src/m4_solvers/roughness.py`) is available and mapped:

| WorldCover Class | Manning n |
|---|---|
| Tree cover (10) | 0.100 |
| Shrubland (20) | 0.065 |
| Grassland (30) | 0.040 |
| Cropland (40) | 0.045 |
| Built-up (50) | 0.080 |
| Bare/gravel (60) | 0.035 |
| Snow/ice (70) | 0.020 |
| Water body (80) | 0.028 |
| Wetland (90) | 0.085 |
| Mangrove (95) | 0.120 |

#### GPU Path

`swe_2d_gpu.py` is complete and tested. It stays **opt-in** (`backend="cpu"` default) because it is **6.6× slower** on the development hardware (GTX 1650, Turing TU117):
- FP64 at 1/32 of FP32 rate on this GPU
- GPU path has no active window (processes full domain)
- Float32 would need re-validation against Ritter due to `h + z` cancellation precision

#### Ritter Benchmark

`src/m4_solvers/ritter.py` implements the Ritter (1892) exact analytical dam-break solution — the standard validation benchmark for SWE solvers. The solver is run against this every time and the RMSE is asserted.

### M5 — Population & Infrastructure Exposure

**File:** `src/m5_exposure/exposure.py`

Per-village exposure computed from the depth raster:

- **Population at Risk (PAR)**: GHS-POP 100 m grid (EC JRC) zonal sum over flooded cells
- **Buildings flooded**: OSM footprints overlaid with depth raster
- **Critical facilities lost**: hospitals + schools + POIs
- **Rupee loss estimate**: JRC global flood damage functions (depth→damage fraction × ₹300k avg replacement)

**Depth-damage curve** (JRC global, residential):

| Depth (m) | 0.0 | 0.5 | 1.0 | 2.0 | 3.0 | 5.0 |
|---|---|---|---|---|---|---|
| Damage fraction | 0.0 | 0.15 | 0.35 | 0.55 | 0.75 | 1.0 |

**Population provenance:** With GHS-POP raster → `COMPUTED_LIVE` (true zonal sum). Without → `PROXY_DATA` (village `pop_total` × flooded area fraction). The label is how the assumption stays visible.

### M6 — Time-Varying Road Isolation ★

**File:** `src/m6_isolation/isolation.py`

**★ THE DIFFERENTIATOR** — answers the question no operational Indian system answers:

> *"Which villages lose their last road out — and when?"*

**Method:**
1. Load OSM road network (cached GraphML), project to depth raster CRS
2. Pre-compute every edge midpoint
3. For each simulation timestep `t`:
   - Sample depth raster at every edge midpoint
   - Cut edges where depth ≥ vehicle threshold
   - Label connected components of surviving graph
   - A village is **ISOLATED** at `t` if its nearest road node shares no component with any safe node
4. **Isolation time** = first `t` at which the village is isolated
5. **Evacuation window** = water_arrival_time − isolation_time

**Depth thresholds (cited):**

| Vehicle Type | Threshold |
|---|---|
| Cars | 0.30 m — most passenger cars stall above this |
| Trucks | 0.50 m — light commercial vehicles |
| Bridges | Same threshold, counted separately (OSM rarely carries deck elevation) |

**Safe set:** Nodes that stay dry for the entire simulation — sampled from the maximum-depth raster, not from an arbitrary bounding box. Self-consistent with solver output.

The evacuation window is the operationally critical metric: a village with a 34-minute window and 1,240 people requires a different response from one with a 3-hour window and 40 people.

### M7 — Priority Ranking

**File:** `src/m7_ranking/ranker.py`

Produces an ordered action list for emergency response. Villages scored on four dimensions:

| Dimension | Weight | Logic |
|---|---|---|
| **Isolation time** | 0.35 | Earlier isolation = higher urgency (inverted) |
| **Population at risk** | 0.35 | More people = higher urgency |
| **Egress capacity** | 0.15 | Fewer road exits = less resilience |
| **Critical facilities** | 0.15 | Hospitals + schools at risk |

Absolute scoring thresholds (isolation): <30 min → 1.0, <60 min → 0.8, <120 min → 0.5, else → 0.1.

Absolute scoring thresholds (PAR): >1000 → 1.0, >500 → 0.8, >100 → 0.5, >0 → 0.2.

Weights are configurable via `RankWeights` dataclass.

### M8 — Output Exporters

**File:** `src/m8_outputs/exporters.py`

| Format | Purpose | Notes |
|---|---|---|
| `.shp` + field-name sidecar | GIS interchange | Truncated column names get a mapping JSON alongside |
| `.kml` | Google Earth / NDMA interchange | via `simplekml` |
| CAP JSON | SACHET-ingestible alert structure | Conformant to Common Alerting Protocol, **NOT** auto-sent |
| GeoTIFF | Max depth, arrival time rasters | LZW-compressed, float32 |

Shapefile export includes a `*_field_map.json` sidecar because Shapefile's 10-character field name limit silently merges columns like `isolation_time_min` and `isolation_computed` — the exporter detects collisions and suffixes numerically.

### M9 — Dashboard

**Files:** `frontend/index.html`, `frontend/map.js`, `frontend/styles.css`, `frontend/ui.js`, `frontend/spine.js`, `frontend/charts.js`, `frontend/debug.js`

A full-featured analyst-facing dashboard:
- **MapLibre GL JS** — full-bleed WebGL map with depth class polygons, road cut timeline, village markers, SAR overlays
- **Plotly** — Ritter benchmark comparison plots, outflow hydrographs with ensemble confidence bands
- **Time scrubber** — three-lane timeline (spine) with event pins and playhead for temporal flood progression
- **Floating glass panels** — CSS Grid overlay with `pointer-events: none` passthrough to the map
- **Run form** — scenario selection, water level, failure mode, resolution, reservoir fraction
- **Answer card** — summary finding with key metrics
- **Evacuation ranking card** — priority-ordered village table with isolation windows
- **Validation overlay** — hit/miss/false alarm heatmap against observed extent
- **Export menu** — download .shp, .kml, CAP JSON, GeoTIFF from the browser
- **Debug console** — on-page log viewer

Design language: dark command-centre aesthetic, `#2F6BFF` accent on chrome only (never on map layers), glassmorphism panels, responsive layout.

### M10 — Validation

**Files:** `src/m10_validation/metrics.py`, `src/m10_validation/observed.py`, `src/m10_validation/roads.py`

Binary extent skill scores against observed data:

| Metric | Formula | Purpose |
|---|---|---|
| **CSI** | TP / (TP + FP + FN) | Critical Success Index — the headline |
| **POD** | TP / (TP + FN) | Probability of Detection |
| **FAR** | FP / (TP + FP) | False Alarm Ratio |
| **F1** | 2TP / (2TP + FP + FN) | Balanced precision/recall |
| **Bias** | (TP + FP) / (TP + FN) | >1 over-predicts, <1 under-predicts |

**Design decisions:**
- TN excluded from CSI (a 99%-dry domain scores 0.99 "accuracy" by predicting nothing)
- Domain mask is **mandatory** — outside the AOI, the observation is *absent*, not *dry*
- Every ratio returns `None` when denominator is zero — never a silent 0.0

**Validation datasets** (shipped in `data/validation/`):
- **Copernicus EMS EMSR696** (Derna, Libya 2023) — 27 GeoJSONs, 17.9 MB: observed extent, road damage, building damage, facilities
- **Global Flood Database** — 7 dam-caused events with observed extent rasters
- **Sen1Floods11** — 446 hand-labelled SAR flood chips
- **Malpasset** — 1959 dam break valley terrain + benchmark mesh + stations

---

## Provenance System

**File:** `src/provenance.py`

Every value that reaches the screen carries one of five labels, set at the point the value is produced:

| Label | Meaning |
|---|---|
| `COMPUTED_LIVE` | Produced by a solver or analysis module during this run |
| `PRECOMPUTED` | Computed by us, offline, before the session |
| `PROXY_DATA` | A real measurement stands in for the one we want |
| `SYNTHETIC_TERRAIN` | Analytic DEM — demo only |
| `NOT_AVAILABLE` | Could not be computed — rendered as a dash, never as 0 |

**Combination rule:** A derived value inherits the **weakest** provenance of its inputs. A live solver on synthetic terrain is still `SYNTHETIC_TERRAIN`. Anything missing dominates everything.

This is how integrity is enforced in code rather than asserted in prose. An earlier build computed isolation as `water_arrival − a 10–35 minute constant` and labelled it "COMPUTED LIVE". That is removed.

---

## Data Sources

| Data | Source | Resolution | How Loaded |
|---|---|---|---|
| DEM | **Copernicus GLO-30** (ESA) | 1 arc-second (~27.9 m) | Windowed COG from public AWS S3, mosaicked, reprojected to UTM |
| Population | **GHS-POP** (EC JRC) | 3 arc-second (~100 m) | Resampled with `sum` onto DEM grid (counts conserved) |
| Settlements | **OpenStreetMap** `place` nodes | Point | Via OSMnx |
| Rivers | **OpenStreetMap** waterways | LineString | Via OSMnx — used to place blockage on main stem |
| Roads | **OpenStreetMap** | Graph | Via OSMnx, cached GraphML |
| Buildings | **OpenStreetMap** footprints | Polygon | 79 for Phutkal (genuinely sparse — reported, not filled) |
| Facilities | **OpenStreetMap** POIs | Point | Hospitals, clinics, schools |
| Land Cover | **ESA WorldCover** (optional) | 10 m | 11-class LULC → Manning roughness |
| Satellite | **Sentinel-1 SAR** + **Sentinel-2 MSI** | 10 m | Via Google Earth Engine (optional) |

All data is fetched by `python -m src.data_fetcher`. Synthetic terrain exists only behind `--offline-demo` and labels everything `SYNTHETIC_TERRAIN`.

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Fetch data and run the pipeline

```bash
# Run with default scenario (Phutkal)
python run_pipeline.py --scenario phutkal --out-dir data/scenarios/demo

# Run Derna (validation case)
python run_pipeline.py --scenario derna --out-dir data/scenarios/derna_run

# Run with 4× downsampled DEM for speed
python run_pipeline.py --scenario phutkal --coarsen 4

# Run with synthetic terrain (no network required)
python run_pipeline.py --scenario phutkal --offline-demo
```

### 3. Start the dashboard

```bash
uvicorn src.api.main:app --reload --port 8000
```

Then open [http://localhost:8000](http://localhost:8000) in a browser.

### 4. Run the test suite

```bash
pytest tests/ -v
```

---

## CLI Reference

```
python run_pipeline.py [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--scenario` | `phutkal` | Scenario key: `phutkal`, `rishiganga`, `derna`, `malpasset`, `ivanovo`, `south_lhonak`, `annamayya` |
| `--dam-name` | (from scenario) | Override dam name string |
| `--wse` | (from scenario) | Water surface elevation [m] |
| `--failure-mode` | `overtopping` | `overtopping` or `piping` |
| `--reservoir-fill` | `0.9` | Reservoir fill fraction (0.0–1.0) |
| `--out-dir` | `data/scenarios/<scenario>_real` | Output directory |
| `--duration` | `7200` (14400 for Annamayya) | Simulation duration [seconds] |
| `--coarsen` | `1` | DEM downsample factor (2 = half resolution, 4× speedup) |
| `--offline-demo` | off | Allow synthetic terrain (labelled `SYNTHETIC_TERRAIN`) |
| `--custom-dem` | none | Path to custom high-resolution DEM GeoTIFF |
| `--crest-length` | none | Dam crest length [m] (CWC calibration) |
| `--dam-type` | none | `earthfill`, `rockfill`, `concrete`, or `masonry` |
| `--spillway-capacity` | none | Controlled spillway capacity [m³/s] |
| `--lulc-path` | none | ESA WorldCover / LULC GeoTIFF for Manning roughness |
| `--population-csv` | none | Local surveyed population CSV override |

---

## API Reference

**Base URL:** `http://localhost:8000`

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/run` | Trigger a live simulation (returns `job_id`) |
| `GET` | `/api/status/{job_id}` | Poll status & progress (includes `cell_size_m`, ETA) |
| `GET` | `/api/results/{job_id}` | Village inundation GeoJSON |
| `GET` | `/api/hydrograph/{job_id}` | `Q(t)` outflow hydrograph with 3-arm confidence bands |
| `GET` | `/api/export/{job_id}/{fmt}` | Download: `shp`, `kml`, `cap`, `geotiff` |
| `GET` | `/api/roads/{job_id}` | `roads_timeline.geojson` with per-link `cut_time_min` |
| `GET` | `/api/arrival/{job_id}` | `arrival_time.tif` (values in minutes, WGS84) |
| `GET` | `/api/scenarios/metadata` | Available scenario metadata (all 7 preconfigured) |

### POST `/api/run` — Request body

```json
{
  "dam_name": "Phutkal River Landslide Dam 2015",
  "scenario_key": "phutkal",
  "wse_m": 3850.0,
  "failure_mode": "overtopping",
  "reservoir_level_fraction": 0.9,
  "duration_s": 7200.0,
  "coarsen": 4,
  "custom_dem_path": null,
  "crest_length_m": null,
  "dam_type": null,
  "spillway_capacity_m3s": null,
  "lulc_raster_path": null,
  "population_csv": null
}
```

The server rehydrates completed runs from `data/scenarios/` on startup, so past simulations survive restarts.

---

## Frontend

All files in `frontend/`. Served by the same FastAPI app — no separate server, no build step.

| File | Purpose |
|---|---|
| `index.html` | Full page — floating header/panels/spine over full-bleed map |
| `styles.css` | All styling, including theme tokens |
| `map.js` | MapLibre setup, all map layers, run/poll lifecycle |
| `spine.js` | Timeline: three lanes, event pins, playhead |
| `ui.js` | Chrome glue — run form, legend, download menu, popover, answer card |
| `charts.js` | Plotly — Ritter plots on the Solvers tab, hydrograph ensemble |
| `debug.js` | On-page debug console |
| `maplibre-gl.*` | Vendored MapLibre GL JS (do not edit) |
| `plotly-2.32.0.min.js` | Vendored Plotly (do not edit) |

**Layout:** The map is `position: fixed; inset: 0` — always full-bleed, full viewport. Chrome floats on a transparent CSS Grid overlay with `pointer-events: none` passthrough so empty gutters click/drag through to the map.

---

## Test Suite

11 test files covering solver correctness, isolation logic, exposure reconciliation, and more:

| Test File | What It Validates |
|---|---|
| `test_swe_validation.py` | All 5 solver benchmarks (lake at rest, mass closure, Ritter RMSE) |
| `test_swe_active_window.py` | Active window reproduces full-domain result **bit for bit** (`==`) |
| `test_swe_gpu.py` | GPU path produces identical results to CPU path |
| `test_ritter.py` | Ritter analytical solution correctness |
| `test_crs.py` | CRS handling and projection consistency |
| `test_overlay_geometry.py` | 12 tests for map polygon smoothing (perimeter excess, fragmentation) |
| `test_bridge_isolation.py` | Bridge edge handling in road isolation |
| `test_ground_connectors.py` | Ground connector logic for road graph |
| `test_exposure_isolation_reconciliation.py` | M5 and M6 consistency |
| `test_lake_cascade_gee.py` | Lake formation, cascade routing, GEE SAR, API endpoints |
| `test_validation.py` | M10 metrics correctness, domain masking, edge cases |

Run with:

```bash
pytest tests/ -v
```

---

## Solver Validation Results

These are the measured, asserted benchmarks from `tests/test_swe_validation.py`:

| Benchmark | Metric | Value | Pass Criterion |
|---|---|---|---|
| Lake at rest | Spurious velocity | 0.0 m/s | Must be exactly zero |
| Lake at rest | Free-surface drift (10 min) | 0.0 m | Must be exactly zero |
| Conical bowl | Mass closure error | < 1% | Well-balanced property |
| Ritter dam-break | Depth RMSE | 0.043 m | < 0.10 m |
| Ritter dam-break | Wave-front position error | 10% | < 15% |

**References:**
- Audusse, E. et al. (2004). A fast and stable well-balanced scheme with hydrostatic reconstruction. *SIAM J. Sci. Comput.*, 25(6), 2050–2065.
- Kurganov, A. & Petrova, G. (2007). A second-order well-balanced positivity preserving central-upwind scheme. *Commun. Math. Sci.*, 5(1), 133–160.
- Ritter, A. (1892). Die Fortpflanzung der Wasserwellen. *Zeitschrift des VDI*, 36, 947–954.

---

## Repository Structure

```
floodsight/
├── run_pipeline.py              # End-to-end simulation (M1→M8)
├── requirements.txt             # Python dependencies
├── README.md                    # This file
├── FLOOD_SYSTEM_REDESIGN_PLAN.md # 110k-word research & planning document
│
├── src/
│   ├── data_fetcher.py          # SCENARIOS config + DEM/OSM/GHS-POP fetchers
│   ├── provenance.py            # 5-label provenance system
│   ├── rasterutils.py           # Shared raster sampling utilities
│   ├── gee_satellite.py         # Google Earth Engine Sentinel-1/2 integration
│   ├── __init__.py
│   │
│   ├── api/
│   │   ├── main.py              # FastAPI app (all endpoints + frontend serving)
│   │   └── routes/
│   │
│   ├── m1_ingest/
│   │   └── national_register.py # Hazard ingest from National Register
│   │
│   ├── m2_geometry/
│   │   ├── dem_utils.py         # DEM conditioning, breach placement
│   │   └── fill.py              # Stage-storage V(h) from DEM fill
│   │
│   ├── m3_breach/
│   │   ├── __init__.py          # DamGeometry, BreachParams, BreachEnsemble
│   │   ├── froehlich.py         # Froehlich (2008) — CWC-recommended
│   │   ├── von_thun.py          # Von Thun & Gillette (1990)
│   │   ├── macdonald.py         # MacDonald & Langemeier (1984)
│   │   ├── ensemble.py          # 3-arm ensemble builder + hydrographs
│   │   └── cascade.py           # Multi-dam cascade routing (Muskingum)
│   │
│   ├── m4_solvers/
│   │   ├── swe_2d.py            # Well-balanced 2D SWE (CPU, active window)
│   │   ├── swe_2d_gpu.py        # GPU port (CuPy, opt-in)
│   │   ├── ritter.py            # Ritter (1892) analytical benchmark
│   │   ├── validation.py        # Lake-at-rest, mass-balance, Ritter comparison
│   │   ├── roughness.py         # LULC → Manning n mapping
│   │   ├── anuga_runner.py      # ANUGA interface (not wired)
│   │   ├── pysph_runner.py      # PySPH interface (not wired)
│   │   └── sph_swe.py           # SPH-SWE hybrid (not wired)
│   │
│   ├── m5_exposure/
│   │   └── exposure.py          # PAR, buildings, facilities, damage curves
│   │
│   ├── m6_isolation/
│   │   └── isolation.py         # Per-timestep road graph cuts ★
│   │
│   ├── m7_ranking/
│   │   └── ranker.py            # Multi-criteria priority ranking
│   │
│   ├── m8_outputs/
│   │   └── exporters.py         # .shp, .kml, CAP JSON, GeoTIFF
│   │
│   └── m10_validation/
│       ├── metrics.py           # CSI, POD, FAR, Bias — binary extent skill
│       ├── observed.py          # Observed extent loading (EMS, GFD, Sen1Floods11)
│       └── roads.py             # Road damage validation against EMS transport links
│
├── frontend/
│   ├── index.html               # Single-page dashboard
│   ├── map.js                   # MapLibre GL JS — all map layers
│   ├── styles.css               # Complete stylesheet
│   ├── ui.js                    # UI glue (forms, legends, exports)
│   ├── spine.js                 # Three-lane timeline
│   ├── charts.js                # Plotly charts
│   ├── debug.js                 # On-page debug console
│   └── assets/                  # Static GeoJSON boundaries
│
├── tests/                       # 11 test files, pytest
│
├── data/
│   ├── admin/                   # Per-scenario villages, rivers, facilities GeoJSON
│   ├── boundary/                # India composite boundary
│   ├── buildings/               # OSM building footprints per scenario
│   ├── dem/                     # Copernicus GLO-30 tiles (git-ignored, fetched)
│   ├── evidence/                # Annamayya data audit & event evidence
│   ├── population/              # GHS-POP tiles (git-ignored, fetched)
│   ├── roads/                   # OSM road GraphML per scenario
│   ├── satellite/               # Sentinel-1 SAR GeoJSON per scenario
│   ├── scenarios/               # Simulation run outputs (git-ignored)
│   └── validation/              # Observed data: EMS, GFD, Sen1Floods11, Malpasset
│
├── docs/
│   ├── SIH26161_Decision_Deck.pptx
│   ├── findings_results.md      # Append-only engineering log
│   ├── memory.md                # Durable design decisions & constraints
│   └── ui.md                    # Frontend layout & design notes
│
├── scripts/
│   ├── grid_convergence.py      # Grid convergence study
│   └── diagnostics/             # Debug helper scripts
│
└── notebooks/                   # Jupyter exploration notebooks
```

---

## Dependencies

### Core Numerics
- `numpy ≥ 1.26`, `scipy ≥ 1.12`

### Raster & Vector GIS
- `rasterio ≥ 1.3`, `shapely ≥ 2.0`, `geopandas ≥ 0.14`, `pyproj ≥ 3.6`, `fiona ≥ 1.9`

### DEM Hydrology
- `pysheds ≥ 0.4`

### Road Graph
- `osmnx ≥ 1.9`, `networkx ≥ 3.2`

### API
- `fastapi ≥ 0.111`, `uvicorn[standard] ≥ 0.29`, `python-multipart ≥ 0.0.9`

### Charts & Visualisation
- `plotly ≥ 5.22`

### Export
- `simplekml ≥ 1.3`, `reportlab ≥ 4.2` (EAP PDF), `lxml ≥ 5.2`

### Data I/O
- `xarray ≥ 2024.2`, `zarr ≥ 2.18`, `pandas ≥ 2.2`, `pyarrow ≥ 16.0`

### Utilities
- `tqdm ≥ 4.66`, `click ≥ 8.1`, `requests ≥ 2.31`, `pydantic ≥ 2.7`

### Optional
- `anuga` — ANUGA hydrodynamic solver (not wired, install separately)
- `pysph` — PySPH SPH solver (not wired, install separately)
- `ee` — Google Earth Engine Python API (for Sentinel SAR integration)

---

## Known Limitations & Honest Gaps

### Not yet implemented
- **ANUGA / PySPH / Delft3D are not wired.** The PS asks for an SPH and a Delft3D arm with a comparison; neither exists yet. This is the honest gap.
- **GEE near-real-time integration** is partially wired (module exists) but not production-tested.

### By design
- **Roads are sparse** in the Phutkal demo AOI (a real property of Zanskar), so isolation usually returns "not isolated" — reported rather than filled in.
- **M2 fill** rejects an unconfined pool and falls back to configured volume, labelled `PROXY_DATA`, when the impoundment would back up past the domain edge.
- **M5 and M6 sample the flood differently.** M5 checks the whole village polygon for `max_depth_m`. M6 samples the centroid. A village can be `inundated: true` with `water_arrival_min: null` — the flood touched the village without reaching M6's sample point. Reported honestly, never fabricated.
- **Only the central breach arm has a timeline.** The two outer arms are envelope-only solves. There are no pessimistic/optimistic arrival times — the ensemble shows spatial extent differences, not temporal ones.
- **Derna validation scores have a ceiling.** Derna 2023 was a compound event (rainfall + dam break). FloodSight routes only the dam break (~23.7 MCM of ~39 MCM total). The gap is rain the model does not simulate, deliberately.
- **GPU path** is 6.6× slower on GTX 1650. Needs different hardware or a windowed float32 port.

---

## PS Deliverable Compliance

| Deliverable | Module | Status |
|---|---|---|
| (i) SPH + Delft3D + comparison | M4 | **Not started** — the honest gap |
| (i) Loss and damage analysis | M5 + M7 | ✅ Wired; JRC damage curves + facility loss |
| (ii) Customisable framework | M1–M4 | ✅ 7-scenario config-driven pipeline |
| (iii) Dashboard + .shp + .kml | M8 + M9 | ✅ Working |
| (iv) GEE near-real-time | M1 + GEE | 🔶 Module exists, not production-tested |
| (v) Indian river + dam demo | — | ✅ Phutkal 2015 + Rishiganga 2021 + South Lhonak 2023 + Annamayya 2021 |

---

## License & Attribution

**Copernicus DEM GLO-30**: © ESA, licensed under the Copernicus DEM Access & Use Terms.

**Copernicus EMS EMSR696**: © European Union, Copernicus Emergency Management Service. Free/open with attribution.

**OpenStreetMap**: © OpenStreetMap contributors, ODbL.

**GHS-POP**: © EC JRC, Creative Commons BY 4.0.

**Global Flood Database**: Tellman et al. (2021), *Nature*.

**Sen1Floods11**: Bonafilia et al. (2020), CVPR Workshops.

---

*SIH26161 · Sponsor: NTRO · Theme: Disaster Management · Deadline: 20 Sep 2026*
