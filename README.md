# FloodSight — SIH26161

> **"NDSA has a plan for all 6,628 specified dams. Nobody has a plan for the dam that formed last Tuesday. We build that in twenty minutes from open data."**

A rapid consequence-assessment engine for **unmapped impoundments** — landslide dams, moraine-dammed lakes, and blocked river reaches — sponsored by NTRO, Smart India Hackathon 2026.

---

## What makes this different from RBSD / C-FLOOD

| Capability | RBSD (C-DAC/NDSA) | C-FLOOD (CWC) | **FloodSight** |
|---|---|---|---|
| Coverage | 6,628 specified dams | 3 river basins | **Any impoundment, including unengineered** |
| Breach ensemble | Single deterministic | N/A | **Froehlich + Von Thun + MacDonald → confidence bands** |
| Time-varying road isolation | ❌ | ❌ | **✅ graph cut per timestep against the depth raster** |
| Access | Login-only | Public | **Analyst-facing, open data** |

RBSD already does dam-break inundation with Population at Risk and Infrastructure at Risk. We do not claim that as novel. What it does not do is *ad-hoc impoundments that have no EAP and no precomputed map*, and it does not cut a road graph over time.

---

## Status — read this before demoing

This project enforces provenance in code, not in prose. Every value carries a
label set where it is produced (`src/provenance.py`), and a derived value
inherits the **weakest** provenance of its inputs.

| Area | State |
|---|---|
| M3 breach ensemble | Working — Froehlich (CWC-recommended), Von Thun, MacDonald |
| M4 2D shallow-water solver | Well-balanced, 2nd order, validated |
| M4 Ritter benchmark | Working — the solver is actually run and compared |
| M5 exposure | Wired. Population is `PROXY_DATA` until a GHS-POP raster is present |
| M6 road isolation | Wired — real OSM graph, real per-timestep cuts |
| M7 ranking | Wired |
| M8 exports | Working — `.shp` (+ field-name sidecar), `.kml`, CAP JSON, GeoTIFF |
| **Terrain** | Copernicus GLO-30, 27.9 m, reprojected to UTM 43N/44N |
| ANUGA / PySPH / Delft3D | **Not wired.** Do not badge them in the UI until they are |

### Solver: what changed and what it measures

The original scheme discretised the bed-slope source independently of the
pressure flux, so they did not cancel. Measured, then fixed:

| Benchmark | Before | After |
|---|---|---|
| Lake at rest, spurious velocity | **20.0 m/s** (saturating the velocity clamp that hid it) | **0.0 m/s** |
| Lake at rest, free-surface drift in 10 min | **39 m** | **0.0 m** |
| Mass closure, conical bowl | **31.5% error** | **< 1%** |
| Ritter RMSE | 0.145 m | **0.043 m** |
| Ritter wave-front error | 26% | **10%** |

The fix is Audusse-style hydrostatic reconstruction on a continuous
piecewise-linear bed, MUSCL + minmod with SSP-RK2, semi-implicit friction, and
Kurganov-Petrova desingularisation. The hard 20 m/s velocity clamp is gone: it
was instability suppression, not physics.

All five numbers are asserted in `tests/test_swe_validation.py`.

### Still open

- **ANUGA / PySPH / Delft3D are not wired.** The PS asks for an SPH and a
  Delft3D arm with a comparison; neither exists yet. This is the honest gap.
- **Roads are sparse** in the demo AOI, so isolation usually returns "not
  isolated" — a real property of Zanskar, reported rather than filled in.
- The M2 fill rejects an unconfined pool and falls back to the configured
  volume, labelled `PROXY_DATA`, whenever the impoundment would back up past
  the domain edge.
- **M5 and M6 sample the flood differently.** M5 (exposure) checks the whole
  village polygon for `max_depth_m`, so a village is `inundated: true` if the
  flood clips any part of its ~800 m buffer circle. M6 (isolation) only
  samples the polygon centroid for arrival time. A village can therefore be
  `inundated: true` with `water_arrival_min: null` — the flood touched the
  village without ever reaching the point M6 measures from. This is reported
  honestly (the frontend renders it as "not computed", never as a fabricated
  time) but the two modules should agree on one sampling rule.

---

## Quick Start

```bash
pip install -r requirements.txt
```

```bash
python run_pipeline.py --scenario phutkal --out-dir data/scenarios/demo
```

```bash
uvicorn src.api.main:app --reload --port 8000
```

```bash
pytest tests/ -v
```

---

## Architecture

```
M1 Hazard Ingest     → National Register + analyst draw       (GEE: not wired)
M2 Geometry          → blockage on the river, seeded fill, V(h) stage-storage
M3 Breach Ensemble   → Froehlich 2008 (CWC) + Von Thun + MacDonald
M4 Solver            → well-balanced 2D SWE, MUSCL + SSP-RK2, Ritter benchmark
M5 Exposure          → village polygons × depth raster
M6 Isolation ★       → OSM graph, per-timestep edge cuts, component reachability
M7 Priority Ranking  → isolation time + PAR + egress + facilities
M8 Outputs           → .shp / .kml / CAP JSON / GeoTIFF
M9 Dashboard         → MapLibre GL JS + Plotly + time scrubber
```

---

## Data — what the code actually loads today

| Data | What the code uses now | Target |
|---|---|---|
| DEM | **Copernicus GLO-30**, windowed COG read from the public AWS mirror, mosaicked and reprojected to UTM (27.9 m cells) | FABDEM bare-earth |
| Population | **GHS-POP** 3 arc-second, resampled with `sum` onto the DEM grid so counts are conserved | — |
| Settlements | **Real OSM `place` nodes** (14 found for Phutkal) | Census 2011 polygons (SHRUG) |
| Rivers | **Real OSM waterways** — used to place the blockage on the main stem | — |
| Roads | **Real OSM** via OSMnx, cached GraphML | — |
| Buildings | **Real OSM footprints** (79 for Phutkal — genuinely sparse) | VIDA (Google + Microsoft + OSM) |
| Facilities | **Real OSM POIs** — zero in this AOI, reported as zero | — |
| Blockage height | Configured in `SCENARIOS`; volume derived from the DEM | NDSA National Register 2026 |

Everything above is fetched by `python -m src.data_fetcher`. Synthetic terrain
still exists but only behind `--offline-demo`, and it labels its output
`SYNTHETIC_TERRAIN` all the way to the screen.

---

## Integrity rules (enforced, not asserted)

Every panel and every row carries one of: `COMPUTED_LIVE`, `PRECOMPUTED`,
`PROXY_DATA`, `SYNTHETIC_TERRAIN`, `NOT_AVAILABLE`.

- A village the simulation does not wet reports `inundated: false` with **null**
  metrics. It is never given an estimated depth or arrival time.
- A village with no road in the OSM graph reports a **null** isolation time and
  renders as NO ROAD DATA.
- We never claim satellite real-time. Sentinel-1 revisits every 12 days outside
  Europe; Sentinel-3 altimetry every ~27 days with ~1.29 m RMSE on rivers.
- We name RBSD and C-FLOOD ourselves and state precisely what we add.

An earlier build computed isolation as `water_arrival − a 10–35 minute constant`,
apportioned population as `pop × (0.6 + depth × 0.12)`, and had a fallback branch
that invented depths and arrival times for villages the flood never reached —
all labelled `COMPUTED LIVE`. That is removed. If a number is not computed, it
is absent.

---

## PS deliverable compliance

| Deliverable | Module | Status |
|---|---|---|
| (i) SPH + Delft3D + comparison | M4 | **Not started** — the honest gap |
| (i) Loss and damage analysis | M5 + M7 | Wired; buildings pending real footprints |
| (ii) Customisable framework | M1–M4 | Scenario-driven |
| (iii) Dashboard + .shp + .kml | M8 + M9 | Working |
| (iv) GEE near-real-time | M1 | **Not started** |
| (v) Indian river + dam demo | — | Phutkal 2015 + Rishiganga 2021 configured |

---

*SIH26161 · Sponsor: NTRO · Theme: Disaster Management · Deadline: 20 Sep 2026*
