# FloodSight — Flood System Redesign Plan

**Research and planning document. No code was modified in producing this.**

**Date:** 31 August 2026
**Subject system:** `D:\sih_work\floodsight` (SIH26161, NTRO, Disaster Management)
**Author basis:** direct source inspection of every module listed in §2, plus the external
research recorded in §4–§11.

**Label key** (continuing the convention of `you-are-a-senior-lucky-harp.md`):

| Label | Meaning |
|---|---|
| `[V]` | Verified — read directly from this repository's source, or from a cited external source I fetched |
| `[I]` | Inference — a reasonable conclusion from verified facts, not itself directly observed |
| `[D]` | Design proposal — mine, not a fact |

**Inspection-depth key** for external projects: `read` = I fetched and read the repo/page ·
`search` = it appeared in search results with a description but I did not open it. Anything
marked `search` should be opened before it is relied on.

---

## Table of contents

1. [Executive summary](#1-executive-summary)
2. [Existing-system audit](#2-existing-system-audit)
3. [Current visualization problems](#3-current-visualization-problems)
4. [Open-source project research](#4-open-source-project-research)
5. [Research-paper and model research](#5-research-paper-and-model-research)
6. [Satellite and data-source comparison](#6-satellite-and-data-source-comparison)
7. [AI model comparison](#7-ai-model-comparison)
8. [Street-scale prediction research](#8-street-scale-prediction-research)
9. [GIS / map technology comparison](#9-gis--map-technology-comparison)
10. [Visualization and animation research](#10-visualization-and-animation-research)
11. [Alert-system research](#11-alert-system-research)
12. [Data feasibility analysis](#12-data-feasibility-analysis)
13. [Benchmark table](#13-benchmark-table)
14. [Recommended architecture](#14-recommended-architecture)
15. [Recommended model stack](#15-recommended-model-stack)
16. [Recommended data pipeline](#16-recommended-data-pipeline)
17. [Recommended map / dashboard design](#17-recommended-map--dashboard-design)
18. [Recommended animation system](#18-recommended-animation-system)
19. [Recommended alert architecture](#19-recommended-alert-architecture)
20. [Validation strategy](#20-validation-strategy)
21. [Performance considerations](#21-performance-considerations)
22. [Risks and limitations](#22-risks-and-limitations)
23. [Keep / Replace / Upgrade / Add / Do Not Build](#23-keep--replace--upgrade--add--do-not-build)
24. [Phased implementation plan](#24-phased-implementation-plan)
25. [Concrete next steps](#25-concrete-next-steps)

---

## 1. Executive summary

### 1.1 The finding that reframes the whole task

The brief assumes the system is a *flood detection and prediction* system whose weakness is
its model. It is not. FloodSight is a **dam-break consequence simulator**: an analyst supplies
an impoundment, and a validated 2D shallow-water solver routes the breach wave over a real
DEM. There is no ML classifier to replace and no forecast skill to improve, because the
system does not forecast — it simulates a hypothesis. `[V]`

That changes the diagnosis. **The dominant problem is not modelling. It is that the system
computes far more than it displays, and displays some things it did not compute.** `[V]`

Three concrete instances, all verified in source:

1. **The differentiator is computed and thrown away.** `compute_isolation_times()`
   ([isolation.py:310](src/m6_isolation/isolation.py:310)) builds a cut road graph `G_cut`
   at every one of the 13 timesteps, labels its connected components, and then discards the
   graph. Only per-village scalars survive. **No road geometry ever reaches the frontend.**
   There is no roads source and no roads layer anywhere in
   [map.js](frontend/map.js) — yet the map legend advertises an orange "Road cut" swatch.
   The single claimed novelty of the project — time-varying road-graph cutting — is invisible
   on the map. `[V]`

2. **The map is fed a degraded copy of data that already exists in better form.**
   The pipeline writes per-timestep depth GeoTIFFs
   ([run_pipeline.py:305](run_pipeline.py:305)), then *also* vectorises each one into
   4-class GeoJSON polygons for the map. The raster is thrown away for display purposes and
   the frontend renders the polygons. Because the flood in a Himalayan gorge is often one
   cell wide and runs diagonally, and because `rasterio.features.shapes` defaults to
   4-connectivity, diagonally-adjacent wet cells do not merge — producing the **staircase of
   disconnected squares** visible in the current screenshot. `[V]`

3. **Two panels display fabricated numbers under a `COMPUTED LIVE` badge.** `[V]`
   - [charts.js:227](frontend/charts.js:227) generates the "ANUGA" and "SWE-SPH" Ritter
     validation curves by adding a **sine wave** to the analytical solution:
     `n = (isSph ? 0.08 : 0.04) * h1 * Math.sin(i * (isSph ? 1.1 : 0.7))`. Neither solver
     runs. This is synthetic validation evidence presented as solver output.
   - [map.js:565](frontend/map.js:565) sets the "Road Edges Cut" counter to
     `Math.round(totalBldg * 1.3)` with the comment `// estimated`.

   The README's own integrity section states that exactly this class of defect was removed
   from an earlier build. It was removed from the *backend*. It survives in the frontend.

### 1.2 What is genuinely good and must be protected

The physics is the strongest asset and is not the problem. The solver is well-balanced
(Audusse hydrostatic reconstruction, MUSCL + minmod, SSP-RK2, Kurganov–Petrova
desingularisation), and the repository asserts five measured benchmark numbers in
[tests/test_swe_validation.py](tests/test_swe_validation.py) — lake-at-rest spurious
velocity 0.0 m/s, mass closure < 1 %, Ritter RMSE 0.043 m. `[V]` The breach ensemble
(Froehlich / Von Thun / MacDonald) and the provenance lattice in
[provenance.py](src/provenance.py) are also sound and rare. Most competing student
submissions will have neither.

### 1.3 The recommendation in one line

> **Do not add a neural network. Render what you already compute, at the resolution you
> already compute it, and stop displaying anything you did not.**

The ranked, honest ordering of impact per unit of effort:

| Rank | Change | Why it wins |
|---|---|---|
| 1 | Delete the two fabricated-data code paths | An integrity failure found by a judge ends the submission. Cost: ~20 lines deleted. |
| 2 | Export the cut road graph per timestep and render it | Turns the claimed differentiator from prose into a visible map layer. The computation already exists. |
| 3 | Replace GeoJSON depth polygons with a COG raster layer | Removes the staircase artefact, gives continuous depth, removes a whole serialization stage. |
| 4 | Add a real basemap stack (satellite / terrain / streets) + hillshade | Answers "where is this?", which the current map does not. |
| 5 | Reconcile M5/M6 sampling, fix the null-rendering bugs | Removes "Evacuation window: **null min**" from the screen. |
| 6 | Raise the live-run resolution ceiling | `coarsen: 6` silently makes live runs 167 m, not 27.9 m. |
| 7 | *Only then* consider Sentinel-1 detection for M1 | It serves hazard *ingest*, not prediction. It is a genuine gap but it is deliverable (iv), not the core. |

Items 1–5 require no new dependency, no new data, and no model training. They are almost
entirely deletions and re-wirings of existing outputs.

### 1.4 The critical-requirement test

The brief sets the bar: *could an emergency operator immediately understand what is flooding,
which roads are affected, where the flood is moving, what will happen next, and who needs
to be warned?*

Against the current build, honestly: **no, on four of five.**

| Question | Current answer | Why |
|---|---|---|
| What is flooding? | Partial | Grey squares on a near-black basemap with no place context |
| Which roads are affected? | **No** | No road layer exists at all |
| Where is it moving? | Partial | 13 discrete frame swaps at 600 ms, no arrival-time surface |
| What happens next? | **No** | No horizon beyond the simulated hour; no confidence shown on the map |
| Who needs warning? | Partial | A ranked list exists, but its top entry currently reads `null min` |

§17–§19 specify the target that turns all five into yes.

---

## 2. Existing-system audit

All statements in this section are `[V]` — read from source. File references are clickable.

### 2.1 Architecture as built

```
run_pipeline.execute_full_simulation()        509 lines — the actual orchestrator
  ├── M1/M2  data_fetcher.get_dem()           Copernicus GLO-30 COG → mosaic → UTM 43N/44N
  │          dem_utils.condition_dem()        pit fill / nodata repair
  │          dem_utils.snap_to_thalweg()      300 m search to correct DEM/OSM misregistration
  │          m2_geometry.fill.build_stage_storage()   seeded flood-fill → V(h) curve
  ├── M3     m3_breach.ensemble.get_hydrographs()     Froehlich + Von Thun + MacDonald
  ├── M4     m4_solvers.swe_2d.run_2d_swe_simulation()  498 lines, well-balanced 2D SWE
  │          m4_solvers.validation.run_ritter_benchmark()
  ├── M5     m5_exposure.exposure.compute_village_exposure()
  ├── M6     m6_isolation.isolation.compute_isolation_times()
  ├── M7     m7_ranking.ranker.rank_villages()
  └── M8     m8_outputs.exporters.export_shp / export_kml / export_cap_json

src/api/main.py   211 lines, FastAPI, 8 endpoints, in-memory _JOBS dict
frontend/         index.html 413 · map.js 650 · charts.js 330 · styles.css 732
```

Total ≈ 6,870 lines. **Not a git repository** — `git status` fails at both `D:\sih_work` and
`D:\sih_work\floodsight`. There is no version control on any of this work.

### 2.2 Answers to the ten audit questions

**(1) What data enters the system?**

| Input | Source in code | Real or synthetic |
|---|---|---|
| DEM | `data_fetcher._copernicus_tile_url()` — GLO-30 COG from the public AWS mirror, windowed read, mosaicked, reprojected to UTM | Real |
| Rivers | OSM waterways via `build_rivers()` | Real |
| Settlements | OSM `place` nodes (`village`/`hamlet`/`town`/`city`) — **14 found for Phutkal** | Real positions, synthetic extents (see below) |
| Population | GHS-POP 3 arc-second, resampled with `sum` so counts are conserved | Real |
| Buildings | OSM footprints — **79 for Phutkal** | Real, genuinely sparse |
| Facilities | OSM POIs — **zero in this AOI**, reported as zero | Real (a real zero) |
| Roads | OSMnx, cached GraphML (`phutkal_roads.graphml`) | Real |
| Blockage height | `SCENARIOS[key]["dam_height_m"]` = 58 m (Phutkal) / 70 m (Rishiganga), from published reporting, not surveyed | Configured constant |
| Impounded volume | Derived from the DEM by seeded fill; falls back to `volume_mcm` constant labelled `PROXY_DATA` when the pool would reach the domain edge | Computed, with honest fallback |

**No satellite imagery enters the system at any point.** There is no Sentinel-1, no
Sentinel-2, no optical, no SAR. GEE is listed as "not wired" in the README and that is
accurate — `grep` finds no Earth Engine call anywhere.

**(2) How is flooding detected?**

It is not detected. It is **simulated**. The analyst asserts an impoundment exists (name,
water-surface elevation, failure mode, fill fraction) and the pipeline computes what happens
if it breaches. There is no detection stage and therefore no detection model.

**(3) How is flood extent represented?**

Three parallel representations, only the weakest of which reaches the map:

| Representation | Where written | Fidelity | Consumed by |
|---|---|---|---|
| `depth_grids` — float32 arrays | in-memory from the solver | Full — continuous depth, native grid | M5, M6 |
| `depth_rasters/depth_NNN.tif` — 13 LZW GeoTIFFs | [run_pipeline.py:305](run_pipeline.py:305) | Full | M6 only |
| `snapshots/frame_NNN.geojson` — 4-class polygons | `_depth_to_geojson()` | **Degraded to 4 bins, 4-connected** | **The map** |

`THRESHOLDS = [0.3, 1.0, 2.0, 3.0]` and the loop emits classes 1–4 for `[0.3,1)`, `[1,2)`,
`[2,3)`, `[3,∞)`. **Water shallower than 0.3 m is never emitted at all** — yet the map legend
shows a `<0.3 m` swatch. The legend's first entry corresponds to nothing on screen.

**(4) How is future flooding predicted?**

By solving the 2D shallow-water equations forward from a breach hydrograph. That is a
physics prediction, and within its assumptions it is a good one. There is **no statistical
or ML forecast**, no rainfall input, no antecedent-conditions model, and no probability
field. Uncertainty is expressed only as the three-arm breach ensemble
(pessimistic / central / optimistic), and **only the central arm is actually routed through
the solver** — `run_2d_swe_simulation(... hydrograph_Q_m3s=cent.Q_m3s ...)`. The
pessimistic and optimistic arms produce hydrographs and ranking bands (`score_lo`,
`score_hi`) but never produce their own inundation maps.

**(5) What spatial resolution is achieved?**

This has two very different answers, and the difference is not surfaced to the user.

| Path | `coarsen` | Effective cell | Grid | Where set |
|---|---|---|---|---|
| CLI (`run_pipeline.py`) | 1 | **27.9 m** | ~1189 × 1134 = 1.35 M cells | argparse default |
| **API / dashboard "Run"** | **6** | **≈ 167 m** | ~198 × 189 = 37 k cells | [main.py:63](src/api/main.py:63) |

The API comment is candid about why — a full-resolution click-to-run took over ten minutes —
but the consequence is that **every number a judge sees from the dashboard is computed on a
167 m grid**. At 167 m, a Himalayan gorge is sub-cell. Nothing at street scale is
representable. The UI does not display the cell size anywhere.

**(6) What temporal resolution is achieved?**

`save_interval_s = 300.0` (5 min) with API `duration_s = 3600.0` (1 h) → **13 frames**.
Matches the observed slider (T+0 … T+60). The solver's internal timestep is CFL-limited and
much finer; only the 5-minute snapshots survive. Isolation times are therefore quantised to
5-minute buckets — a village "isolated at T+35 min" means "first observed isolated in the
T+35 snapshot", with up to 5 minutes of error. This quantisation is not stated on screen.

**(7) What map technology is used?**

MapLibre GL JS **4.1.3** from unpkg CDN, Plotly 2.32, Google Fonts. Style is an inline
`version: 8` object with exactly one source: raw `tile.openstreetmap.org` raster tiles,
desaturated via paint properties (`raster-saturation: -0.35`, and in dark mode
`raster-brightness-max: 0.55`, `raster-opacity: 0.7`).

Notable absences: **no terrain, no hillshade, no `raster-dem` source of any kind.** The
file header comment says "MapLibre GL JS 3D terrain map" — this is not accurate. `pitch: 40`
tilts a flat raster plane; there is no elevation in the scene. `[V]`

**(8) How are roads / buildings / water bodies represented?**

- **Roads** — loaded as an OSMnx GraphML, cut per timestep in M6, **never rendered**. There
  is no road layer.
- **Buildings** — 79 OSM footprints, used for the loss calculation, **never rendered**.
- **Water bodies / rivers** — `data/admin/phutkal_rivers.geojson` exists on disk and is used
  to place the blockage on the main stem. **Never rendered.**
- **Villages** — rendered, but as `Point(x, y).buffer(0.008)` in EPSG:4326
  ([data_fetcher.py:376](src/data_fetcher.py:376)). 0.008° ≈ **890 m** at 33 °N. These are
  the large grey circles in the screenshot. They are not village extents; they are uniform
  discs around OSM point nodes, and the buffer is taken in degrees so it is slightly
  elliptical.

**(9) How are predictions displayed to the user?**

Left panel: hazard inputs, breach ensemble cards, four live counters, export buttons.
Centre: map + time scrubber + legend + a single isolation callout.
Right panel: priority ranking list + Plotly outflow hydrograph.
Two extra full-page tabs: Solver Comparison, About.

**(10) What makes the visualization hard to interpret?**

See §3 — this is the substance of the brief and gets its own section.

### 2.3 Provenance system — the good part

[provenance.py](src/provenance.py) defines an ordered lattice
`COMPUTED_LIVE > PRECOMPUTED > PROXY_DATA > SYNTHETIC_TERRAIN > NOT_AVAILABLE` with a
`worst()` combinator, and the pipeline applies it per row:

```python
def _row_provenance(row) -> str:
    return str(worst(Provenance.COMPUTED_LIVE, terrain_prov, row.get("pop_provenance")))
```

A live solver on synthetic terrain is correctly still labelled synthetic. This is a genuinely
good design and is rarer than it should be. **The backend honours it. The frontend does not**
(§3.1). The fix is to make the frontend obey a system that already exists, not to build a
new one.

### 2.4 Verified defect register

Ordered by severity. Every entry was read in source.

| # | Severity | Location | Defect |
|---|---|---|---|
| D1 | **Blocking** | [charts.js:223–239](frontend/charts.js:223) | ANUGA and SWE-SPH Ritter curves are the analytical solution plus `Math.sin()` noise. Neither solver runs. Rendered under `COMPUTED LIVE`. |
| D2 | **Blocking** | [map.js:565](frontend/map.js:565) | `val-roads` = `Math.round(totalBldg * 1.3)` — a fabricated count under `COMPUTED LIVE`. |
| D3 | **Blocking** | [index.html:57–59](frontend/index.html:57) + Solver Comparison tab | Header badges "ANUGA / SWE-SPH / Delft3D" and the tab marks two of them `COMPUTED LIVE`. `grep` finds **zero callers** of `anuga_runner`, `pysph_runner`, `sph_swe` outside their own directory. README explicitly warns: *"Do not badge them in the UI until they are [wired]."* |
| D4 | High | [map.js:512](frontend/map.js:512) | `showIsolationCallout` interpolates `p.evacuation_window_min` raw instead of via the existing `fmtWin()` helper → renders the literal string **`null min`**. Visible in the current screenshot. The helper directly above it carries a comment saying null must never render as text. |
| D5 | High | [map.js:322](frontend/map.js:322) | `_checkIsolationAtFrame`: `tMin >= p.isolation_time_min` where the value is `null` coerces to `tMin >= 0` → **always true**. Combined with a single global `isolationShown` flag, exactly one village ever pops, and it can be one that never isolates. This is why Dorzong shows "Last road closes at *not computed*". |
| D6 | High | M5 vs M6 | M5 tests the whole 890 m village disc for `max_depth_m`; M6 samples only the polygon centroid for arrival. A village can be `inundated: true` with `water_arrival_min: null`, or vice versa. Two modules, two sampling rules, one screen. |
| D7 | High | [main.py:63](src/api/main.py:63) | `coarsen: 6` on the live path (≈167 m) is never disclosed in the UI. |
| D8 | Medium | [map.js:229](frontend/map.js:229) | Depth `step` expression uses ramp indices `0,0,1,2,4` — index 3 (`--depth-4`) is dead, so the ramp has a hole and is non-monotonic in perceived steps. |
| D9 | Medium | [map.js:245](frontend/map.js:245) | Flood outline colour hardcoded `#67e8f9`, identical to `--depth-1`, and not theme-aware. On class-1 cells the outline is invisible against its own fill; elsewhere it outlines *every* polygon rather than the wave front. |
| D10 | Medium | [charts.js:244](frontend/charts.js:244) | `renderDemoHydrograph()` hardcodes `Hw = 42`, `Vw = 250e6` — invented figures for a demo path, the same defect class the README says was removed. |
| D11 | Medium | [map.js:432](frontend/map.js:432) | `onTimeSlider` demo fallback hardcodes `maxMin = 360` and a magic isolation threshold `tMin >= 47`. Dead path with invented numbers. |
| D12 | Medium | [map.js:568](frontend/map.js:568) | "Villages Isolated" counter is `0 < evacuation_window_min < 60` — that is *short evacuation window*, not *isolated*. Mislabelled. |
| D13 | Medium | [index.html:352–378](frontend/index.html:352) | About tab claims FABDEM, VIDA, SHRUG, GHS-POP 100 m. The code actually loads Copernicus GLO-30, OSM buildings, OSM place nodes, GHS-POP 3 arc-sec. Stale claims. |
| D14 | Medium | [main.py:8](src/api/main.py:8) | Docstring: `/api/results` returns "village inundation **& road cuts**". It returns no road cuts. |
| D15 | Low | [map.js:277](frontend/map.js:277) | All 13 frames pre-fetched in a **serial** `for` loop — 13 sequential round-trips before the scrubber becomes usable. |
| D16 | Low | [ranker.py:96](src/m7_ranking/ranker.py:96) | `priority_score` is min-max normalised *within the run*, so it is not comparable across scenarios and rarely approaches 1.0 (observed rank-1 = **0.35**). The map's priority colour ramp interpolates 0→0.5→1.0, so **no village ever reaches the urgency colour**. Every disc renders in the pale low end. |
| D17 | Low | repo root | **No git repository.** No history, no branches, no ability to bisect a regression before a 20 Sep deadline. |

---

## 3. Current visualization problems

Diagnosed against the supplied screenshot and confirmed against source.

### 3.1 The screenshot, read as evidence

The current frame at T+30 min shows: a near-black basemap; a diagonal chain of ~30 pale
axis-aligned squares; two large translucent grey discs; a small orange callout reading
*"Dorzong — Last road closes at **not computed** · Water arrives at **T+20min** · Evacuation
window: **null min** · 13 people"*; a legend advertising a teal depth ramp plus an orange
"Road cut" and a dark "Village isolated" swatch, **neither of which appears anywhere on the
map**; and a time scrubber badged `COMPUTED LIVE`.

Every one of those is traceable to a specific line of code.

### 3.2 P1 — The staircase artefact

**Symptom:** flood renders as disconnected squares rather than a continuous water body.

**Cause chain** `[V]` → `[I]`:
1. Cell size on the live path is ≈167 m (`coarsen: 6`).
2. In a gorge the wet band is often 1–2 cells wide.
3. `_depth_to_geojson` calls `rasterio.features.shapes(...)`, which defaults to
   **4-connectivity**. Diagonally-adjacent cells are separate shapes.
4. `unary_union` merges only *touching* polygons; corner-touching ones remain separate.
5. The valley runs NW→SE, so the wet cells form a diagonal staircase → a chain of
   individually-outlined squares. `[I]`, but the geometry in the screenshot matches exactly.

**Aggravators:** the outline layer strokes *every* polygon in `#67e8f9`, adding a grid to the
staircase; and the deepest class (`>3 m` → `--depth-5` `#1d4ed8`, ~15 % relative luminance)
sits on a basemap dimmed to `brightness-max: 0.55` in dark mode. **The most dangerous water is
the least visible.** The ramp is inverted in salience.

**This is not fixable by tuning the vectoriser.** Any raster-to-polygon step at this cell size
produces blocks. The fix is to stop vectorising for display (§9.3).

### 3.3 P2 — No geographic context

The basemap is OSM raster, desaturated to −0.35, dimmed to 0.55 brightness and 0.7 opacity.
Labels are barely legible in the screenshot; terrain is entirely absent. For a Himalayan
gorge, **terrain is the single most explanatory layer** and it is the one thing not drawn,
despite the pipeline already having the DEM in hand.

There is no basemap switcher — no satellite, no terrain, no hybrid. An operator cannot answer
"is that the road or the river?" from this map.

### 3.4 P3 — The road layer does not exist

The most serious visualization gap. The legend promises it; the computation produces it; the
map has no source and no layer for it. §2.1 D2/D14 cover the code. The operational
consequence: **the question "which roads are affected?" has no answer on the screen at all.**

### 3.5 P4 — Villages are 890 m discs

They read as "flood blobs" rather than settlements, they overlap the depth polygons and
mute them (fill-opacity 0.16 grey over the water), and because `priority_score` never exceeds
~0.35 (D16) every disc renders in the same pale colour. The ranking is invisible on the map
even though it is the right-hand panel's entire subject.

### 3.6 P5 — Null leakage

`null min` on screen (D4), a callout for a village that never isolates (D5), and a `<0.3 m`
legend entry for a class that is never emitted. The project's stated integrity rule is
"if a number is not computed, it is absent" — the frontend violates it in three separate ways
while displaying a `COMPUTED LIVE` badge.

### 3.7 P6 — The animation is a slideshow

`setInterval(..., 600)` swapping whole GeoJSON payloads. 13 hard cuts over 8 seconds. No
interpolation, no easing, no directional cue. The eye cannot track a wave front through a
hard swap of 30 polygons; it perceives flicker, not propagation. There is also no
**arrival-time** rendering — the single most useful static summary of a moving flood — even
though `water_arr_t` is computed per village and could be computed per cell from the same
raster stack for near-zero cost.

### 3.8 P7 — No uncertainty on the map

Three breach arms are computed. One is routed. The map shows a single crisp edge with no
indication that the pessimistic arm puts that edge somewhere else. `score_lo`/`score_hi`
exist in `results.geojson` and appear nowhere in the UI. A crisp line communicates a
confidence the model does not have.

### 3.9 P8 — Information hierarchy is inverted

The largest, brightest, most persistent UI element on the left is the *input form*. The
ranked list of who dies first is a narrow right-hand column with 11 px detail text. For an
operator, the priority ordering **is** the product; the inputs are set once. The layout
optimises for the demo-driver, not the decision-maker.

---

## 4. Open-source project research

I searched GitHub and the literature across the categories in the brief. Below are only the
projects with meaningful technical evidence, with the inspection depth honestly marked.

### 4.1 Hydraulic / inundation engines

| Project | Depth | What it is | Licence | Relevance to FloodSight |
|---|---|---|---|---|
| **[Deltares/SFINCS](https://github.com/Deltares/SFINCS)** | `read` | Reduced-complexity compound-flood solver. Up to **100× faster than Delft3D** for equivalent domains with negligible accuracy loss in sub-critical flow. Subgrid + efficient spatial discretisation. Set up via HydroMT-SFINCS (Python). 83 ★, 43 forks, active issues. | **GPL-3.0** source; Windows/Docker binaries under a separate Deltares *Freeware* licence (non-commercial modification prohibited) | **High.** A credible third solver arm that is actually installable, unlike Delft3D-FLOW. But note: SFINCS is *sub-critical* reduced-physics. A landslide-dam breach in a steep gorge is **super-critical** — the regime where SFINCS's simplification is weakest. Use it as a comparison arm on a benign reach, not as the primary. `[I]` |
| **[NOAA-OWP/inundation-mapping](https://github.com/NOAA-OWP/inundation-mapping)** | `search` | Operational US national FIM. **HAND** grids + reach-averaged **synthetic rating curves** at HUC-8 scale. Docker workflows. A full HAND set is ~1.7 TB. | US Government open source | **Medium-high for method, low for code.** The *architecture idea* — precompute a terrain-derived HAND surface once, then convert a streamflow forecast into inundation by table lookup in milliseconds — is exactly the right pattern for FloodSight's "20 minutes from open data" pitch. The code is bound to the US National Water Model. |
| **[NOAA-OWP/ras2fim](https://github.com/NOAA-OWP/ras2fim)** | `search` | Builds inundation raster libraries + rating curves from HEC-RAS models | US Gov OSS | Low — HEC-RAS dependency. |
| **[sdmlua/FIMserv](https://github.com/sdmlua/FIMserv)** | `search` | Wrapper to generate FIMs quickly for emergency response | Check before use | Medium — worth opening for its service-layer design. |
| **[csdms-contrib/fwdet](https://github.com/csdms-contrib/fwdet)** | `search` | Floodwater Depth Estimation Tool: depth from an inundation **polygon** + DEM | CSDMS | **Medium.** Relevant only if satellite-derived extent is added (§8.4) — converts a Sentinel-1 extent mask into a depth field. Accuracy caveats in §5.3. |
| **Inunda** ([arXiv 2607.09614](https://arxiv.org/abs/2607.09614)) | `read` (abstract) | GPU-native, **differentiable** 2D SWE with a mass-conservative **local-inertial** scheme. Runs multi-day events over millions of cells in minutes on one GPU. Autograd-compatible operators enable gradient-based parameter estimation (e.g. recovering hydraulic conductivity fields). | **Not stated** in the abstract; no repo link given | **Watch, do not depend.** The differentiable-solver direction is the most interesting thing in 2026 flood modelling, but with no confirmed repo or licence this is not a dependency you can plan a 20 Sep deadline around. |
| **ANUGA** | already in repo | 2D FV SWE, unstructured mesh; the solver inside C-FLOOD (C-DAC/NSM) | Open | `anuga_runner.py` exists with zero callers. Wiring it is real work (mesh generation from raster, boundary conditions), not a badge. |
| **PySPH** | already in repo | SWE-SPH, IIT Bombay Aerospace, BSD | BSD | Same — `pysph_runner.py` has zero callers. The in-repo `sph_swe.py` (1D, self-contained, no PySPH dependency) is the more realistic path. |

### 4.2 Forecasting systems

| Project | Depth | Evidence |
|---|---|---|
| **Google Flood Hub** / [google-research hydrology framework](https://research.google/blog/the-next-chapter-in-flood-resilience-open-sourcing-googles-hydrology-framework/) | `search` | LSTM river-stage forecasting, **7-day lead time** with reliability comparable to the best available nowcasts. Coverage: 100 countries verified + up to 150 with virtual gauges; **all of India and Bangladesh**. As of June 2025, 80+ countries, 1,800+ sites, 460 M people. Google has **open-sourced the hydrology framework**, trainable on the open **Caravan** dataset. |
| | | **Implication for FloodSight, stated plainly:** Google already does gauge-and-basin-scale river forecasting for all of India, better than a hackathon team will. Do not compete there. FloodSight's defensible ground is exactly what Flood Hub does *not* cover: an impoundment that formed last week, has no gauge, no rating curve, and no history to train on. Reinforces the existing positioning rather than changing it. |
| **ECMWF Code4Earth 2026 Challenge 11** — "Interactive map-based dashboard for flood forecasting" | `search` | An open challenge on this exact UI problem. Worth reading for its requirements list, which is effectively a peer-reviewed spec for §17. |

### 4.3 Map / visualization tooling

| Project | Depth | Why it matters here |
|---|---|---|
| **[geomatico/maplibre-cog-protocol](https://github.com/geomatico/maplibre-cog-protocol)** | `read` | **The direct fix for P1.** Registers a `cog://` protocol; MapLibre fetches byte-ranges from a Cloud Optimized GeoTIFF and decodes in-browser — no tile server. Modes: image, DEM (hillshade + Terrain-RGB 3D), **colour ramp on single-band rasters with continuous or discrete interpolation** (ColorBrewer / CARTOColors), and custom per-pixel functions with access to all bands. MIT. `npm i @geomatico/maplibre-cog-protocol`. **Constraint: the COG must be EPSG:3857 — the library does not reproject.** Requires MapLibre 4.5 / 5 / 6; use `tileSize: 256`. |
| **[opengeos/maplibre-gl-time-slider](https://github.com/opengeos/maplibre-gl-time-slider)** | `search` | Time-series raster/vector slider for MapLibre; builds TiTiler XYZ URLs for COGs. Reference implementation for §18 even if not adopted wholesale. |
| **[opengeos/maplibre-gl-raster](https://github.com/opengeos/maplibre-gl-raster)** | `search` | Local + remote raster visualisation plugin. Alternative to the above. |
| **[Protomaps / PMTiles](https://protomaps.com/api)** | `search`+ | Single-file vector-tile archive served over HTTP byte-range; read natively by MapLibre via `addProtocol`. Daily OSM planet builds (~111 GB); the `pmtiles` CLI **extracts a bbox or GeoJSON region from the remote build without downloading the planet**. A mid-sized country lands in the low GB; dropping maxzoom 15→14 roughly halves it. |
| **maplibre `color-relief` layer** | `read` (style spec) | Native layer type, **added in MapLibre GL JS v5.6.0**. "Client-side elevation colouring based on DEM data" — renders a hypsometric tint from Terrain-RGB tiles on the GPU. Paint properties: `color-relief-color`, `color-relief-opacity`. **The project is on 4.1.3 and cannot use this without upgrading.** |

### 4.4 Ideas extracted (not just names)

1. **HAND + rating-curve lookup** (NOAA-OWP) — precompute terrain once, make the runtime step a lookup. Directly applicable to making a live run fast *without* coarsening to 167 m.
2. **Byte-range COG in the browser** (maplibre-cog-protocol) — deletes the entire GeoJSON serialization stage and the staircase with it.
3. **Region extract from a remote planet build** (PMTiles) — a self-hosted, offline-capable, key-less basemap for one AOI at a few hundred MB.
4. **Subgrid representation** (SFINCS) — coarse compute grid carrying fine-scale conveyance. The principled alternative to `coarsen: 6`.
5. **Differentiable solver for parameter estimation** (Inunda) — the credible way to calibrate Manning's *n* against an observed 2015 extent instead of guessing three constants.

---

## 5. Research-paper and model research

### 5.1 SAR flood segmentation — the benchmark picture

**Sen1Floods11** is the standard benchmark: 446 hand-labelled 512×512 chips across 11 flood
events, 14 biomes, 6 continents, providing Sentinel-1 VV/VH **and** Sentinel-2 optical.
`[V, search]`

| Model family | Reported result | Source | Depth |
|---|---|---|---|
| ViT / CNN-ViT hybrid | **IoU 0.72** on the Sen1Floods11 test set (multi-class) | [arXiv 2606.16302](https://arxiv.org/abs/2606.16302) | `search` |
| SegFormer-b2 vs U-Net | SegFormer clearly better on ETCI; **after fine-tuning on Sen1Floods11 the gap narrows**, and the remaining advantage concentrates on *spatially fragmented* flood events | same | `search` |
| Prithvi-EO-1.0-100M | Water IoU **79.6 %** | [HF model card](https://huggingface.co/ibm-nasa-geospatial/Prithvi-EO-1.0-100M-sen1floods11) | `search` |
| **Prithvi-EO-2.0-600M-TL** | Water IoU **83.1 %** | [arXiv 2412.02732](https://arxiv.org/pdf/2412.02732) | `search` |
| DeepSARFlood | ViT-based **deep ensembles with uncertainty estimates**, automated SAR FIM | ScienceDirect, 2025 | `search` |

**The two conclusions that matter for this project:**

1. **The architecture race is nearly flat.** The spread between a well-trained U-Net and a
   2026 transformer on this benchmark is single-digit IoU. A team choosing SegFormer over
   U-Net gains far less than a team fixing its preprocessing or its labels. `[I]`
2. **Prithvi's flood checkpoint is Sentinel-2 optical (HLS), not SAR.** `[V]` For a monsoon
   flash flood under cloud, the optical model is unavailable exactly when it is needed.
   Cloud-independence is the reason SAR exists. Any detection arm must be SAR-first with
   optical as a clear-sky bonus.

Also relevant: *"Land cover and flood type govern the detection limits of satellite-based
flood mapping"* ([arXiv 2606.07780](https://arxiv.org/pdf/2606.07780)) `search` — the limits
are set by terrain and flood type, not by model choice. **In steep, radar-shadowed Himalayan
terrain, SAR flood detection is at its hardest.** `[I]` This is a direct warning against
promising Sentinel-1 detection for the Zanskar AOI specifically.

### 5.2 Temporal / surrogate flood prediction

| Approach | Finding | Source | Depth |
|---|---|---|---|
| LSTM–U-Net surrogate over HEC-HMS/HEC-RAS | Hybrid: LSTM for temporal hydrological memory, U-Net for spatial inundation pattern | [Water 18(11) 1360](https://doi.org/10.3390/w18111360) | `search` |
| SMDFN encoder–decoder, tightly-coupled 1D/2D via manhole discharge, residual + spatial + channel attention | High-resolution urban inundation forecasting | [J. Hydrology, Mar 2026](https://www.sciencedirect.com/science/article/abs/pii/S0022169426003689) | `search` |
| Physics-informed DL for rapid urban inundation | PINN-style constraint on the surrogate | [J. Hydrology 2024](https://www.sciencedirect.com/science/article/abs/pii/S0022169424013945) | `search` |
| DL super-resolution over a **coarse-grid** hydrodynamic run | Coarse physics + learned upsampling = rapid high-res inundation | [Eng. App. CFD 2025](https://www.tandfonline.com/doi/full/10.1080/19942060.2025.2481115) | `search` |
| Flood-LDM | Latent diffusion, **zero-shot** high-resolution flood mapping | [arXiv 2511.14033](https://arxiv.org/pdf/2511.14033) | `search` |
| DL + rain-on-grid hydrodynamics, **Guwahati, India** | Catchment-scale flood forecasting; DL rainfall forecast feeding a hydrodynamic model | [J. Flood Risk Mgmt 2026](https://onlinelibrary.wiley.com/doi/10.1111/jfr3.70186) | `search` |

**The one directly transferable idea:** *coarse hydrodynamic grid + learned super-resolution*.
That is a principled, published alternative to `coarsen: 6`. But it requires a training set of
paired coarse/fine runs, which means **running the fine solver many times first** — the very
thing that is too slow. It is a Phase 4 idea, not a deadline idea. `[I]`

**The blunt assessment on ConvLSTM / PredRNN / 3D-CNN / neural operators for *this* system:**
these are surrogates that learn a mapping a simulator already provides. FloodSight *has* the
simulator, it is validated to Ritter RMSE 0.043 m, and it has **zero training data** — no
observed depth time-series for Phutkal 2015 exists. Training a spatiotemporal net on
synthetic output from your own solver produces a model that is, at best, a lossy compression
of the solver, and cannot be validated independently. **Recommend against.** `[D]`

### 5.3 Depth-from-extent (FwDET)

Reported accuracy: mean difference **0.18 m** coastal (1 m DEM) and **0.31 m** riverine
(10 m DEM). `[V, search]` But a 2025 comparison reports FwDET RMSE **5.23 m** against a TSA
method's 0.805 m, and the method is known to produce **discontinuous depth fields with linear
striping** and large bias where the extent polygon does not align with the DEM. `[V, search]`

**Conclusion:** usable to turn a satellite extent into a first-order depth estimate, clearly
labelled `PROXY_DATA`. **Not** usable as a validation target for the hydrodynamic solver —
its error is an order of magnitude larger than the solver's Ritter RMSE.

### 5.4 Road-network flood impact — the literature FloodSight is already in

This is where the project's existing M6 sits, and the literature confirms the design is sound.

| Work | Finding | Depth |
|---|---|---|
| **STGCN for road-network inundation status** ([arXiv 2104.02276](https://arxiv.org/pdf/2104.02276)) | Predicts near-future road inundation **2–6 hours ahead** for situational awareness | `search` |
| Web-based decision support for road accessibility + emergency facility allocation ([Urban Informatics 2024](https://link.springer.com/article/10.1007/s44212-024-00040-0)) | Uses **OSMnx + NetworkX ego-graphs** before/after flooding — the same toolchain FloodSight already uses | `search` |
| Routing optimisation under **time-varying** flood vulnerability ([ASCE JITSE 2026](https://ascelibrary.org/doi/10.1061/JITSE4.ISENG-2788)) | Flood-level → speed-reduction functions; arcs progressively removed over time | `search` |
| Road accessibility during floods, facility-based indices ([2025](https://www.sciencedirect.com/science/article/pii/S2212420925002997)) | **≥ 20 cm inundation ⇒ road inaccessible to passenger vehicles** | `search` |
| Road disruption → access to **emergency medical services** ([Sci. Tot. Env. 2024](https://www.sciencedirect.com/science/article/pii/S0048969724072978)) | Spatiotemporal vulnerability framing | `search` |

Two concrete takeaways:

1. **The 0.30 m car threshold in `THRESH_CAR` is defensible but at the conservative end.**
   The 2025 study uses **0.20 m**. Make it a UI control with both values cited, rather than a
   buried constant. `[D]`
2. **A graded speed-reduction function beats a binary cut.** The literature models
   degradation, not just severance. FloodSight's binary cut is simpler and honest; a
   `PASSABLE / SLOW / IMPASSABLE` three-state is a small, well-supported upgrade. `[D]`

### 5.5 DeepINDRA

Confirmed to exist: *"DeepINDRA: An experimental system for forecasting street-scale flood
inundation by coupling physical and deep learning models"*, listed on the Government of India
[ISTI Portal](https://www.indiascienceandtechnology.gov.in/research/deepindra-experimental-system-forecasting-street-scale-flood-inundation-coupling-physical-and-deep).
`[V, search]`

**The detail page was unreachable** (`connect ECONNREFUSED 164.100.228.160:443`) and follow-up
searches surfaced adjacent Indian work (the Guwahati rain-on-grid + DL study) but not
DeepINDRA's own methodology, resolution, or lead time. **I could not verify its architecture
and am not going to characterise it from its title.** It is flagged in §25 as an open
verification item — and it matters for competitive positioning, because a
government-catalogued Indian street-scale flood system is prior art the judges may know.

**The name itself is the useful signal:** "coupling physical and deep learning models"
confirms that the accepted Indian research direction is **hybrid physics + AI**, not pure ML.
FloodSight is already on the physics half of that axis with a validated solver. That is a
strong starting position, not a deficit.

---

## 6. Satellite and data-source comparison

### 6.1 Sentinel-1 (SAR) — with a correction to the repo's own claim

The README states: *"Sentinel-1 revisits every 12 days outside Europe."* **That was true from
December 2021 to April 2025 and is no longer current.** `[V]`

| Period | Constellation | Revisit |
|---|---|---|
| 2016 – Dec 2021 | S1A + S1B | 6 days |
| Dec 2021 – Apr 2025 | S1A only (S1B power failure) | **12 days** |
| Dec 2024 | S1C launched | — |
| From 26 Mar 2025 | S1C acquiring regularly | returning toward 6 days |
| 4 Nov 2025 | S1D launched | — |
| From 17 Apr 2026 | S1D calibrated data open | **nominal 6-day revisit being restored** on the final S1C/S1D configuration |

Sources: [ASF HyP3 Sentinel-1](https://hyp3-docs.asf.alaska.edu/sentinel1/),
[Copernicus Data Space](https://documentation.dataspace.copernicus.eu/Data/SentinelMissions/Sentinel1.html),
[ESA facts and figures](https://www.esa.int/Applications/Observing_the_Earth/Copernicus/Sentinel-1/Facts_and_figures).

**Fix the README number.** It is the kind of stale fact a domain-expert judge catches, and
it undercuts a document whose whole authority rests on being carefully sourced.

**But the conclusion is unchanged and must be stated just as firmly:** 6 days is still not
real-time. A landslide dam can form and breach inside that window. Sentinel-1 is a
**hazard-discovery** sensor for FloodSight, not a monitoring one, and the honest framing is
*"we can find impoundments that persist for a week or more"*, never *"we watch them live"*.

Additional Himalaya-specific caveat `[I]`: steep terrain produces radar layover and shadow,
and SAR water detection degrades exactly where the AOI is. Any Sentinel-1 arm must be
demonstrated on a wide, flat reach (Kosi, Assam) before it is claimed for Zanskar.

### 6.2 Sentinel-2 (optical)

10 m for RGB+NIR, 20 m SWIR. Water indices: NDWI, MNDWI, AWEI. Cloud-limited — and the
Indian flood season is the cloud season. Best used for the **before** half of a before/after
comparison and for validating a SAR mask on a clear day. Prithvi's flood checkpoint operates
here (HLS), which is why Prithvi is a supporting model and not the primary (§5.1).

### 6.3 DEM comparison

| DEM | Resolution | Type | Accuracy evidence | In use? |
|---|---|---|---|---|
| **Copernicus GLO-30** | 30 m (27.9 m at this latitude) | DSM (surface) | Global radar-DEM evaluation reports **0.55 m error on bare ground** vs NASADEM's 1.5 m. Among the **least slope-sensitive** DEMs, having been derived from higher-resolution source data | **Yes** |
| **FABDEM** | 30 m | **DTM** (bare-earth) | Built *from* Copernicus GLO-30 DGED with a random-forest correction that removes forest and building bias. **Ranked first** in a five-DEM flood-prone-environment assessment, ahead of Copernicus | No (aspirational in About tab) |
| SRTM / NASADEM | 30 m | DSM | Copernicus is more accurate than SRTM; NASADEM improves on SRTM (1.5 m bare ground) but is still behind GLO-30 | No |
| AW3D30 | 30 m | DSM | Also among the least slope-impacted | No |

Sources: [Int. J. Digital Earth 17(1) 2024](https://www.tandfonline.com/doi/full/10.1080/17538947.2024.2308734),
[JGR Biogeosciences 2024](https://agupubs.onlinelibrary.wiley.com/doi/full/10.1029/2023JG007672). `[V, search]`

**Assessment for this AOI:** FABDEM's advantage is removing **forest and building** bias.
Zanskar above 3,500 m is near-treeless and has 79 buildings in the whole AOI. **The FABDEM
upgrade would buy this scenario almost nothing** `[I]`, and it carries a CC BY-NC-SA licence
that complicates a government submission. Keep Copernicus GLO-30. It is the right choice and
the About tab should be corrected to say so rather than the upgrade being pursued.

The DEM constraint that actually binds is different: **30 m is 30 m.** No amount of model
sophistication produces street-scale flood depth from a 30 m DSM. §8 and §12 treat this as
the hard limit it is.

### 6.4 Population

**GHS-POP** at 3 arc-second (~100 m at the equator), resampled with `sum` onto the DEM grid so
counts are conserved — this is already implemented correctly and is the right choice for
India per the IIHS 2024 assessment the team cites. `[V]` No change recommended.

### 6.5 Indian national sources

| Source | What it offers | Depth |
|---|---|---|
| **[Bhuvan API](https://bhuvan-app1.nrsc.gov.in/api/)** (ISRO/NRSC) | Thematic layers integrable into third-party apps: disasters, water resources, land cover | `search` |
| **NDEM** (National Database for Emergency Management) | Flood hazard zonation, current + historic flood maps, flood duration, annual layers | `search` |
| **National Flood Vulnerability Assessment System** (on Bhuvan) | Grid-level topography, rainfall trend, runoff, flood vulnerability index nationally | `search` |
| **[data.gov.in rainfall catalogue](https://www.data.gov.in/catalog/rainfall)** | Station data (CWC, Gujarat WRD, APWRIMS) + **gridded IMD and NRSC** rainfall | `search` |

**Strategic note:** for an **NTRO** sponsor, showing the system consuming a Bhuvan/NDEM layer
is worth more than a marginally better foreign model. It demonstrates the system fits the
Indian data estate. Cheapest credible version: add Bhuvan as a **selectable basemap** and cite
NDEM historical flood layers as the validation reference for §20. Both are UI-layer work.

---

## 7. AI model comparison

Scored **for FloodSight specifically**, not in the abstract.

### 7.1 Segmentation (would serve M1 hazard ingest)

| Model | Input | Reported | Params | Verdict for FloodSight |
|---|---|---|---|---|
| **U-Net** | S1 VV/VH | Benchmark baseline | ~30 M | **Recommended if a detection arm is built.** Trains on one GPU, easy to debug, and §5.1 shows the transformer gap is small after fine-tuning. |
| U-Net++ | S1 | Marginal over U-Net | ~35 M | Not worth the extra complexity. |
| DeepLabV3+ | S1/S2 | Comparable to U-Net | ~40 M | No advantage here. |
| **SegFormer-b2** | S1 | Beats U-Net on ETCI; **gap narrows after Sen1Floods11 fine-tune**, advantage concentrated on fragmented floods | ~25 M | **Second choice.** A gorge flood *is* spatially fragmented, so the reported advantage is in the right regime — but on a Himalayan AOI SAR itself is the bottleneck (§6.1), not the head. |
| Swin / Mask2Former | S1/S2 | SOTA-class | 60–200 M | **No.** Cost and training data are out of proportion to a binary water mask. |
| **SAM / SAM2** | optical | Promptable, zero-shot | ~600 M | **No.** SAM segments *objects*; SAR water is a low-backscatter texture, not an object. Wrong inductive bias, and SAM2's video tracking has no analogue on a 6-day revisit. |
| **Prithvi-EO-2.0-300M-TL-Sen1Floods11** | **Sentinel-2 / HLS** | **Water IoU 83.1 %** (600M-TL) | 300–600 M | **Best zero-training option, wrong sensor.** Fine-tuned checkpoint on Hugging Face, ships with TerraTorch. Optical ⇒ cloud-blocked in monsoon. Use as a **clear-sky cross-check** on a SAR mask, never as the primary. |
| DeepSARFlood | S1 | ViT deep ensembles **with uncertainty estimates** | — | **Highest-value pattern to copy** even if the code is not used: an ensemble that returns a *calibrated uncertainty* fits FloodSight's provenance ethos exactly, where a single hard mask does not. |

### 7.2 Temporal prediction

| Model | Verdict |
|---|---|
| ConvLSTM / ConvGRU / PredRNN / 3D-CNN | **Do not build.** No training data exists for this problem (§5.2). Would learn the existing solver and could not be validated independently. |
| Spatiotemporal transformers | Same, with a larger data appetite. |
| **STGCN on the road graph** | **The one worth keeping on the roadmap.** Predicts road inundation status 2–6 h ahead on a graph — the natural structure for M6, and a graph has orders of magnitude fewer degrees of freedom than a raster. Still needs training data FloodSight does not have; revisit only if a multi-event archive is assembled. |
| Neural operators (FNO/DeepONet) | Elegant for parametric PDE families; needs thousands of solver runs to train. Phase 4 at the earliest. |
| **PINNs** | **No.** PINNs are worst on discontinuous solutions. A dam-break wave front is a shock. This is the documented failure mode of the method. |
| **DL super-resolution over coarse hydrodynamics** | **The most promising hybrid** (§5.2) and the principled replacement for `coarsen: 6`. Requires paired coarse/fine runs — buildable *because* you own the solver. Phase 4. |

### 7.3 The overall model recommendation

> **Ship zero new neural networks before 20 September 2026.**

The physics arm is validated and the ML arms have no training data, no validation target,
and no capability the physics does not already provide. The one defensible ML addition —
SAR segmentation for M1 — serves *hazard discovery*, is a separate deliverable (iv), and
is at its weakest in the demo AOI (§6.1). Adding it under deadline pressure risks the
integrity story that is the project's actual differentiator.

---

## 8. Street-scale prediction research

### 8.1 The honest answer for this AOI

The brief asks the system to say *"this road will be impassable in 45 minutes"*. Assessed
against what this system actually has:

| Requirement | Available? | Evidence |
|---|---|---|
| Road geometry | **Yes** | Real OSM GraphML, cached |
| A depth field over that geometry through time | **Yes** | 13 depth GeoTIFFs |
| A rule converting depth → passability | **Yes** | `THRESH_CAR = 0.30` m, and the literature supports 0.20–0.30 m |
| The graph-cut computation | **Yes, already running** | `cut_flooded_edges()` per timestep |
| **A way to see any of it** | **No** | No road layer exists |
| Terrain resolution to trust it *per street* | **No** | 30 m DEM, 167 m live grid |

So: **"this road becomes impassable at T+45 min" is computable today and just is not
displayed.** But "this *street* floods to 30 cm" is not, and will not be, at 30 m terrain.

The distinction is the whole of §8:

- **Road-link-level impact — deliverable now.** A 30 m grid resolves a valley road as a
  cut/not-cut question. That is exactly what M6 computes and exactly what a Zanskar operator
  needs, because there is one road.
- **Street-level urban depth — not deliverable at 30 m, ever.** Urban street flooding needs
  ~1 m LiDAR and a drainage network. Neither exists for this AOI, and India-wide 1 m LiDAR
  is not open data.

**Promise the first. Never the second.** §12 makes this a formal table.

### 8.2 The AOI's own honesty problem

Phutkal/Zanskar contains **14 settlements, 79 buildings, and zero facilities** `[V]`. Road
isolation "usually returns not isolated" because the network is a single sparse chain. A
street-scale demo here is not just infeasible — it has no streets.

**This is a scenario-selection problem, not a modelling problem.** `[D]` The system is
scenario-driven; adding a third scenario on a **populated Indian valley reach with a real
road network** (e.g. a Rishiganga-downstream reach through Tapovan/Joshimath, or a Kosi
2008 reach in Bihar) would let the road-isolation differentiator actually fire, with real
buildings, real facilities, and a real graph to cut. This is config work — a new `SCENARIOS`
entry plus a data fetch — not new code. It is the highest-leverage demo change available.

### 8.3 Methods that raise effective resolution without new data

| Method | Applicability | Note |
|---|---|---|
| **HAND** (Height Above Nearest Drainage) | High | Terrain-normalised height; the basis of NOAA-OWP FIM. Cheap from the existing DEM (`pysheds` is already a dependency). Gives a *precomputed* inundation-vs-stage surface so runtime becomes a lookup. |
| **Flow accumulation / D8 or D-infinity** | High | Already available via `pysheds`. Would let the flood layer be constrained to hydrologically plausible paths and would visibly kill stray isolated cells. |
| **Subgrid conveyance** (SFINCS-style) | Medium | The principled fix for `coarsen: 6` — coarse compute grid carrying fine-scale conveyance. Real implementation effort. |
| **Depth super-resolution CNN** | Low (Phase 4) | Needs paired training runs. |
| Urban 1D/2D drainage coupling | **Not applicable** | No drainage network data for a Himalayan valley. |

### 8.4 Should this be a physics + AI hybrid?

**It already is the physics half, and the physics half is the half that is validated.** `[D]`

The right hybrid for FloodSight is not "physics feeding a neural net". It is:

```
AI for perception   →  where is the impoundment, how big  (M1 — SAR segmentation, later)
Physics for dynamics →  what happens when it breaks       (M4 — exists, validated)
Graph for consequence →  who loses their road out, when   (M6 — exists, invisible)
```

AI belongs on the **input** side (finding hazards), not on the dynamics side (where a
validated solver already sits). Inverting that trades a benchmarked solver for an
unvalidatable surrogate.

---

## 9. GIS / map technology comparison

### 9.1 Client libraries

| Library | Verdict for FloodSight |
|---|---|
| **MapLibre GL JS** | **Keep — but upgrade 4.1.3 → ≥ 5.6.** Already in use, open, no API key, WebGL. The upgrade is not cosmetic: `color-relief` (v5.6) is the layer type that solves the depth-rendering problem natively. |
| Mapbox GL JS | **No.** Proprietary licence + mandatory API key. A government-facing submission should not depend on a metered foreign key. |
| Leaflet | **No.** No WebGL; would be a downgrade for raster/vector volume. |
| OpenLayers | Strong projection support (useful given the COG EPSG:3857 constraint) but would mean rewriting the whole frontend. Not worth it. |
| **deck.gl** | **Optional overlay for one job.** `TripsLayer` / `PathLayer` for animated road-status and particle-style flow. Interoperates with MapLibre. Adds a large dependency — justify per feature, do not adopt wholesale. |
| Cesium | **No.** 3D globe is spectacle, not decision support, and the terrain data to justify it does not exist here. |
| Kepler.gl | **No.** An exploration tool, not an embeddable operational dashboard. |

### 9.2 Server side

| Tech | Verdict |
|---|---|
| **COG** (Cloud Optimized GeoTIFF) | **Adopt.** The pipeline already writes GeoTIFFs; adding `TILED=YES` + overviews makes them COGs. Near-zero cost. |
| **PMTiles** | **Adopt for the basemap.** Single file, byte-range served, no tile server, works offline. Extract the AOI from the remote planet build with the `pmtiles` CLI. |
| TiTiler | Only if dynamic server-side rasters are needed. `maplibre-cog-protocol` avoids it entirely for static COGs. **Prefer the no-server path.** |
| GeoServer | **No.** Heavyweight Java stack for a static output set. |
| PostGIS | **Not yet.** The output is a handful of files per job. Add only when scenarios must be queried across runs. |
| Vector tiles for results | **Not needed.** 14 villages and a few hundred road edges are trivially small as GeoJSON. Vector tiles matter for the *basemap*, not for these results. |

### 9.3 The specific fix for the depth layer

Three viable routes, ranked:

**Route A — `color-relief` on a Terrain-RGB-encoded depth raster.** `[D]` *Recommended.*
Encode depth into a Terrain-RGB PNG/COG, add it as a `raster-dem` source, and use a
`color-relief` layer with `color-relief-color` as the depth ramp. Continuous colour, GPU
interpolation, no polygons, no staircase, no class quantisation. **Requires MapLibre ≥ 5.6.**

**Route B — `maplibre-cog-protocol` colour-ramp mode.** MIT, `cog://` protocol, byte-range
fetch, ColorBrewer ramps with continuous interpolation, works on MapLibre 4.5/5/6.
**Constraint: the COG must be reprojected to EPSG:3857** — the pipeline currently writes UTM
43N/44N, so this adds one `rasterio.warp` step per frame. Route B is the fallback if the
MapLibre upgrade proves disruptive.

**Route C — keep GeoJSON but fix the vectoriser** (`connectivity=8`, more classes, simplify,
smooth). Cheapest, and still yields polygons at 167 m. **Palliative, not a fix.**

Take A. Fall back to B. Do not settle for C.

---

## 10. Visualization and animation research

### 10.1 Colour and symbology guidance

From emergency-cartography practice `[V, search]`: red/purple = bad to very bad; yellow/orange
= something is happening; the traffic-light convention is publicly understood and draws the
eye immediately; bold red signals danger without cultural interpretation and survives poor
lighting. NAPSG's incident symbology guideline and ArcGIS's real-time emergency monitoring
docs are the reference points.

**Applying this honestly against the existing design.** The repo's `map.js` contains a
thoughtful comment arguing *against* a green-amber-red ramp for priority, on the grounds that
it reads as three categories and fails for red-green colour blindness. **That reasoning is
correct for a continuous magnitude and I would keep it.** But it has been over-applied: road
status is **genuinely categorical** (open / at risk / impassable / no data), it is a
decision variable, and it is the case the traffic-light convention was built for.

The resolution `[D]`:
- **Continuous magnitudes** (depth, priority score) → single-hue sequential ramp. Keep.
- **Categorical status** (road passability) → traffic light, **plus a non-colour channel**
  (dash pattern / line width) so it survives colour blindness. Both, not either.

### 10.2 Depth ramp — the specific fix

The current ramp ends at `--depth-5: #1d4ed8` (~15 % luminance) on a basemap dimmed to
`brightness-max: 0.55`. **Deep water is dark on a dark map — the most dangerous cells are the
least salient.** `[V]` The ramp must run **light-and-desaturated → dark-and-saturated on a
light basemap**, or invert for dark mode. And it must be checked against both basemaps, since
the app has a theme toggle that currently changes the map's contrast without re-checking the
ramp.

### 10.3 The animation problem

Current: `setInterval(600 ms)` hard-swapping 13 GeoJSON payloads. Perceptually this is
flicker, not propagation.

What the literature and practice converge on `[D]`:

1. **Arrival-time surface as the primary static view.** One raster: for each cell, the first
   timestep its depth crosses threshold. It answers "where is it moving and how fast" in a
   single frame, with no animation at all, and it is **derivable from the existing raster
   stack with one pass** — the same reduction M6 already performs for villages, applied
   per-cell. This is the highest-value new visualization in the entire plan and it costs
   almost nothing.
2. **Continuous time, not frame indices.** Slider in minutes; cross-fade between adjacent
   frames. GPU opacity cross-fade between two raster layers is cheap and reads as motion.
3. **Persistent wave-front trace.** Draw the leading edge with a short trailing history so
   direction is visible in a still screenshot.
4. **Timeline event markers.** Mark the moment each village isolates directly on the scrubber
   track, so the operator sees *when the decisions happen* before pressing play.

### 10.4 The T−2h → NOW → +2h concept

The brief asks for a past→now→future scrubber. **State plainly what this system's time axis
actually is:** FloodSight has **no "now"**. Its axis is T+0 = breach initiation, running
forward. There is no observed past and no assimilated present.

Presenting a T−2h segment would be fabrication of exactly the kind §2.4 flags. `[D]` The
honest version of the same UX:

```
BREACH ──── +15m ──── +30m ──── +45m ──── +60m
   │                     ▲
   └ T+0                 └ scrubber
        ● Dorzong isolated      ● Testa isolated
```

Same affordance, same legibility, zero invented history. If a real "now" is ever wanted, it
requires the M1 detection arm plus data assimilation — a different project.

---

## 11. Alert-system research

### 11.1 What exists in the repo

`export_cap_json()` produces a CAP-structured payload, and the UI correctly states *"CAP JSON
is structured for SACHET ingestion — NOT transmitted."* `[V]` That restraint is right and
should be preserved: claiming a live SACHET integration that does not exist would be the same
class of error as D1–D3.

### 11.2 What an alert should contain

Practice guidance `[V, search]`: accuracy, timeliness, clear messaging; consistency across
sirens/mobile/social; and the recognition that a technically sound warning fails if people
cannot tell what to do. Design the payload around **who reads it**:

**Public (SMS/WhatsApp, multilingual, ≤160 chars):**
> Flood warning: Dorzong. Water expected ~20 min. Move uphill now, away from the river.
> Do not use the river road. — District Admin

No depths, no probabilities, no model names. One action.

**Emergency responder:**
> HIGH · Dorzong (33.24 N, 76.93 E) · 13 people · water T+20 min · road cut T+35 min ·
> evacuation window 15 min · max depth 33 m · 6 buildings · 0 facilities
> Confidence: central breach arm; pessimistic arm brings water forward to T+14 min.
> Provenance: COMPUTED LIVE (Copernicus GLO-30, 167 m grid, 5-min snapshots)

**Administrator dashboard:** the ranked table, sortable, with the provenance column visible
and every uncomputed value rendered as an explicit dash — never `null`, never an estimate.

### 11.3 The rule that must not be broken

**An alert may only contain values the pipeline computed.** Today the frontend would happily
put `null min` into an evacuation window (D4). An alert is a higher-stakes surface than a
map. The alert builder must read from the same provenance-stamped row the map reads, and must
**refuse to emit** a field whose provenance is `NOT_AVAILABLE`. That is a validation rule in
code, not a convention.

### 11.4 Channels — realistic scope

| Channel | Feasible by 20 Sep? | Note |
|---|---|---|
| CAP JSON export | **Already done** | Keep the "not transmitted" label |
| Web dashboard alert panel | **Yes** | Pure frontend |
| Browser push | Yes, low value | Nobody watches this in a browser |
| SMS / WhatsApp | **No** | Requires a gateway, a registered sender, and authorisation FloodSight does not have. Showing a *rendered preview* of the SMS is honest and demonstrates the design; sending is out of scope. |
| Multilingual (Hindi / Ladakhi / Urdu) | **Yes, and worth it** | Static template translation. Cheap, and it reads as seriousness about the actual affected population. |

---

## 12. Data feasibility analysis

The core discipline the brief demands: separate what can be predicted from what would merely
look good.

### 12.1 Feasibility table

| Capability | Required data | Available? | Resolution | Update freq. | Source | Verdict |
|---|---|---|---|---|---|---|
| Breach hydrograph Q(t) | Dam height + volume + failure mode | **Yes** | n/a | per run | DEM-derived V(h) + Froehlich/VonThun/MacDonald | **Ship.** Working |
| Flood extent (simulated) | DEM + hydrograph | **Yes** | 27.9 m CLI / **167 m live** | per run | Copernicus GLO-30 + SWE solver | **Ship**, disclose the live cell size |
| Flood depth (simulated) | same | **Yes** | same | 5-min snapshots | same | **Ship**, render as raster not polygons |
| **Flood arrival time (per cell)** | existing raster stack | **Yes — one reduction away** | same | derived | already on disk | **Ship. Highest value / lowest cost item in this plan** |
| **Road link cut + cut time** | OSM graph + depth stack | **Yes — already computed, discarded** | link-level | 5 min | OSMnx + M6 | **Ship. This is the differentiator** |
| Village isolation time | above + component labelling | **Yes** | village | 5 min | M6 | Ship; fix the M5/M6 sampling mismatch first |
| Population exposure | GHS-POP + depth | **Yes** | ~100 m | static | GHS-POP 3 arc-sec | Ship |
| Buildings at risk | OSM footprints + depth | **Yes, but sparse** | building | static | OSM (79 in AOI) | Ship with the count stated, not hidden |
| Critical facilities | OSM POI + depth | **Yes — and it is zero here** | POI | static | OSM | Ship the honest zero |
| Direct loss (₹) | depth-damage curve + unit cost | **Partly** | building | per run | JRC-shaped curve, **flat ₹300,000/structure** | Ship **labelled `PROXY_DATA`** — the unit cost is an assumption, not a survey |
| Breach uncertainty band | 3 breach arms | **Computed, only 1 routed** | n/a | per run | M3 | **Upgrade:** route all three arms, show an extent envelope |
| **Flood probability %** | ensemble over uncertain inputs | **No** | — | — | — | **Do not display.** A single deterministic run cannot yield "82 % in 2 hours". Fabricating it destroys the integrity story |
| **Satellite-detected current extent** | Sentinel-1 + segmentation | **Not wired**, feasible | 10–20 m | **6 days** (2026) | Copernicus | Phase 3+. Never call it real-time |
| **Depth from satellite extent** | S1 mask + DEM (FwDET) | Feasible | 30 m | 6 days | FwDET | Phase 4, `PROXY_DATA`, RMSE caveats (§5.3) |
| **Rainfall-driven forecast** | IMD gridded rainfall + hydrology | Data exists (data.gov.in), model does not | ~0.25° | daily | IMD/NRSC | **Out of scope.** Different problem, and Google Flood Hub already covers India |
| **Street-level urban depth** | ~1 m LiDAR + drainage network | **No** | — | — | — | **Do not build. Do not promise** |
| Live gauge / river stage | in-situ telemetry | **No** for unmapped impoundments — *that is the premise of the project* | — | — | — | Correctly out of scope |

### 12.2 The one-line summary

**Can be delivered:** simulated depth, extent, arrival time, road cuts, isolation windows,
population and building exposure, ranked priorities, ensemble bands — all at **30 m terrain,
5-minute steps, over a 1–2 hour horizon, for an analyst-specified breach.**

**Cannot be delivered and must not be shown:** flood probability percentages, real-time
satellite monitoring, street-level urban depth, rainfall-driven forecasting, live gauge
assimilation.

---

## 13. Benchmark table

Only systems with real technical evidence. Inspection depth is marked; `search`-only rows
should be opened before being relied on.

| System | Depth | Detection | Prediction | Model | Satellite | Street-level | Animation | Roads | Depth output | Alerts | Open source |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **FloodSight (today)** | `read` | ✗ none | Physics 2D SWE, 1 h | Well-balanced FV SWE | ✗ | ✗ (167 m live) | Frame swap | **Computed, not rendered** | ✓ continuous, shown as 4 bins | CAP JSON, not sent | n/a (no VCS) |
| **FloodSight (planned §14)** | — | Phase 3+ | Physics + ensemble band | same + 3 arms | Phase 3+ | Link-level only | Cross-fade + arrival surface | **✓ rendered, time-varying** | ✓ continuous raster | CAP + previews | Recommend Apache-2.0 |
| **RBSD** (C-DAC/NDSA) | prior doc | ✗ | Precomputed dam-break | — | ✗ | ✗ | ✗ | ✗ | ✓ | ✓ | ✗ login-only |
| **C-FLOOD** (CWC/C-DAC) | prior doc | ✗ | 48 h, village level | ANUGA on NSM HPC | ✗ | ✗ | ? | ✗ | ✓ | ✓ | ✗ |
| **Google Flood Hub** | `search` | ✗ | **7-day** river stage | LSTM (framework open-sourced) | for maps | ✗ | ✓ | ✗ | ✓ | ✓ | Framework yes, service no |
| **NOAA-OWP inundation-mapping** | `search` | ✗ | 10-day, NWM-driven | HAND + synthetic rating curves | ✗ | ✗ | ✗ | ✗ | ✓ | ✓ | ✓ US Gov |
| **Deltares SFINCS** | `read` | ✗ | Hydrodynamic | Reduced-physics, subgrid | ✗ | ✓ w/ fine DEM | ✗ (engine) | ✗ | ✓ | ✗ | ✓ GPL-3.0 (binaries: freeware) |
| **Inunda** | `read` abs | ✗ | Hydrodynamic + **differentiable** | GPU local-inertial | ✗ | ✓ w/ fine DEM | ✗ | ✗ | ✓ | ✗ | **Unstated** |
| **Prithvi-EO-2.0 Sen1Floods11** | `search` | **✓ IoU 83.1 %** | ✗ | ViT foundation, 300–600 M | **S2/HLS optical** | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ HF + TerraTorch |
| **DeepSARFlood** | `search` | ✓ **+ uncertainty** | ✗ | ViT deep ensembles | **S1 SAR** | ✗ | ✗ | ✗ | ✗ | ✗ | ? |
| **STGCN road inundation** | `search` | ✗ | **✓ 2–6 h, road status** | Spatiotemporal GCN | ✗ | ✓ road-level | ✗ | **✓** | ✗ | ✗ | Paper |
| **FwDET** | `search` | ✗ | ✗ | Geometric | via extent | ✗ | ✗ | ✗ | ✓ (high RMSE) | ✗ | ✓ CSDMS |

### 13.1 What this table says

**No system in it does time-varying road-graph cutting driven by a hydrodynamic depth stack.**
STGCN predicts road inundation but from observation, not from a breach simulation. NOAA-OWP
and SFINCS produce depth but touch no road graph. FloodSight's claimed differentiator survives
this benchmark intact — **the gap is that it is not rendered.**

---

## 14. Recommended architecture

### 14.1 The shape

Deliberately close to what exists. Bold = new or rewired.

```
   ANALYST INPUT                    [Phase 3+] SENTINEL-1
   name · WSE · mode · fill                    ↓
        ↓                            SAR water segmentation (U-Net)
        │                                      ↓
        │                            candidate impoundment polygon
        ↓                                      ↓
   ┌────┴──────────────────────────────────────┴────┐
   │  M1/M2 TERRAIN                                  │
   │  Copernicus GLO-30 → UTM → condition → V(h)     │
   │  + HAND, flow accumulation  ← NEW               │
   └────┬────────────────────────────────────────────┘
        ↓
   ┌─────────────────────────────────────────────────┐
   │  M3 BREACH ENSEMBLE                             │
   │  Froehlich · Von Thun · MacDonald               │
   │  → THREE hydrographs                            │
   └────┬────────────────────────────────────────────┘
        ↓
   ┌─────────────────────────────────────────────────┐
   │  M4 2D SWE SOLVER (unchanged, validated)        │
   │  ROUTE ALL THREE ARMS  ← CHANGED                │
   │  → depth stack ×3, COG-tiled  ← CHANGED         │
   │  → ARRIVAL-TIME RASTER  ← NEW                   │
   └────┬────────────────────────────────────────────┘
        ↓
   ┌────┴────────────┬───────────────────────────────┐
   │  M5 EXPOSURE    │  M6 ISOLATION                 │
   │  pop/bldg/fac   │  graph cut per timestep       │
   │  SHARED         │  EMIT CUT EDGE GEOMETRY  ←NEW │
   │  SAMPLING RULE  │  + per-link cut time     ←NEW │
   │  ← CHANGED      │                               │
   └────┬────────────┴───────────────┬───────────────┘
        ↓                            ↓
   ┌─────────────────────────────────────────────────┐
   │  M7 RANKING — absolute scale, not min-max ←CHG  │
   └────┬────────────────────────────────────────────┘
        ↓
   ┌─────────────────────────────────────────────────┐
   │  M8 OUTPUTS                                     │
   │  .shp · .kml · CAP JSON · GeoTIFF               │
   │  + roads_timeline.geojson   ← NEW               │
   │  + arrival_time.cog          ← NEW              │
   │  + depth_NNN.cog (EPSG:3857) ← NEW              │
   └────┬────────────────────────────────────────────┘
        ↓
   ┌─────────────────────────────────────────────────┐
   │  M9 DASHBOARD — MapLibre ≥5.6                   │
   │  basemap switcher · hillshade · color-relief    │
   │  depth · ROAD STATUS LAYER · arrival isochrones │
   │  · ensemble envelope · alert panel              │
   └─────────────────────────────────────────────────┘
```

### 14.2 Why this architecture

**Why not a multi-modal encoder → segmentation → spatiotemporal → constraints stack (the
brief's example)?** Because every stage of it needs training data this problem does not have,
and it would replace a solver validated to Ritter RMSE 0.043 m with a surrogate that has no
independent validation target. The proposed architecture keeps the validated physics and
spends the effort on **making its output legible and complete** — the actual deficit.

**Why route all three breach arms?** Because the ensemble is the project's honest answer to
uncertainty, it is already computed, and routing three arms costs 3× solver time — which at
the *current* live settings is under a minute. It converts a crisp, over-confident flood edge
into a defensible envelope. Best uncertainty-per-unit-effort available.

**Why HAND and flow accumulation?** `pysheds` is already a dependency. HAND gives a
precomputed stage→inundation surface (the NOAA-OWP pattern), which is the credible route to
raising the live resolution without raising the wall time. Flow accumulation constrains the
flood to hydrologically plausible paths.

### 14.3 Expected characteristics

| Property | Value |
|---|---|
| Spatial resolution | 27.9 m batch; target ≤ 56 m live (`coarsen: 2`) with HAND assist |
| Temporal resolution | 5 min snapshots; **60 s** for the road timeline (cheap — it is a raster sample + graph op) |
| Forecast horizon | 1–2 h simulated from breach initiation. **No "now", no assimilation** |
| Uncertainty | 3-arm breach envelope. **No probability percentages** |
| Training required | **None** |
| Inference | CPU. GPU optional |
| Hard limit | 30 m DEM ⇒ link-level roads, never street-level urban depth |

---

## 15. Recommended model stack

| Layer | Choice | Why | Phase |
|---|---|---|---|
| Breach | Froehlich (CWC-recommended) central + Von Thun + MacDonald | Already built; Froehlich is the Indian regulator's own recommendation | ✓ exists |
| Hydrodynamics | **Existing well-balanced 2D SWE** | Validated on 5 asserted benchmarks. Replacing it would be a regression | ✓ exists |
| Terrain preprocessing | **+ HAND, + flow accumulation** (`pysheds`) | Dependency already present; unlocks fast lookup and plausible flow paths | 2 |
| Comparison arm A | **`sph_swe.py` 1D SWE-SPH, actually wired** | In-repo, self-contained, no PySPH install risk. Satisfies PS deliverable (i) honestly on the Ritter benchmark | 2 |
| Comparison arm B | **SFINCS** on a benign reach | GPL-3.0, installable, 100× faster than Delft3D. Note super-critical caveat (§4.1) | 3 |
| Delft3D | **Keep `PRECOMPUTED`, or drop the badge** | A live Delft3D run is a known high-risk failure. Precomputed is honest; badging it live is not | — |
| SAR detection | U-Net on Sentinel-1 VV/VH, Sen1Floods11 fine-tune | Transformer gap is small (§5.1); U-Net trains and debugs on one GPU | 4 |
| Optical cross-check | Prithvi-EO-2.0-300M-TL-Sen1Floods11 via TerraTorch | Zero training; clear-sky only | 4 |
| Depth from extent | FwDET, `PROXY_DATA` | Only for satellite-derived extents | 4 |
| **Everything else** | **Not built** | ConvLSTM, PredRNN, PINN, FNO, SAM, Mask2Former, super-resolution: no training data, no validation target, no capability the physics lacks | — |

---

## 16. Recommended data pipeline

```
STATIC, CACHED ONCE PER AOI
  Copernicus GLO-30 COG ──→ mosaic ──→ UTM ──→ condition ──→ dem.tif
                                                   ├──→ hand.tif          NEW
                                                   └──→ flowacc.tif       NEW
  OSM (Overpass/OSMnx) ──→ roads.graphml · buildings · places · waterways · POIs
  GHS-POP 3 arc-sec ────→ sum-resample onto the DEM grid ──→ pop.tif
  Protomaps planet ─────→ pmtiles extract (AOI bbox) ──→ basemap.pmtiles   NEW

PER RUN
  inputs ──→ M2 seeded fill ──→ V(h)
         ──→ M3 ensemble ──→ Q_pess(t), Q_cent(t), Q_opti(t)
         ──→ M4 × 3 arms ──→ depth stacks
                ├──→ depth_NNN_{arm}.cog        (EPSG:3857, tiled+overviews)   NEW
                ├──→ arrival_time_{arm}.cog                                     NEW
                ├──→ max_depth_{arm}.cog
                └──→ extent_envelope.geojson    (pess ∪ opti outline)           NEW
         ──→ M5 exposure ─┐
         ──→ M6 isolation ─┤ SHARED sampling rule                               CHANGED
                           ├──→ roads_timeline.geojson  (per-link cut time)     NEW
                           └──→ village rows
         ──→ M7 ranking (absolute scale)                                        CHANGED
         ──→ M8 .shp · .kml · CAP JSON · GeoTIFF · alert previews
```

**Deleted from the pipeline:** `snapshots/frame_NNN.geojson` and `_depth_to_geojson()`.
Once the map reads COGs, the vectoriser has no consumer. That is ~30 lines removed, one
output directory removed, and the staircase artefact removed with it.

**Format notes.**
- COGs need `TILED=YES`, internal overviews, and — for `maplibre-cog-protocol` — EPSG:3857.
  Reproject at the **output boundary only**; every internal computation stays in UTM metres,
  exactly as the pipeline already does for its WGS84 outputs.
- `roads_timeline.geojson`: one feature per road link, with `cut_time_min` (null if never
  cut), `is_bridge`, `highway`, and the provenance label. Small — a few hundred features.
- `arrival_time.cog`: single band, minutes, nodata where never wet.

---

## 17. Recommended map / dashboard design

### 17.1 Layout

Current layout gives the most space to the input form and the least to the ranked list. Invert.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ FloodSight   [Dashboard][Solvers][About]      COMPUTED LIVE · 56 m · ±5m │  ← cell size + Δt VISIBLE
├────────────┬────────────────────────────────────────────┬────────────────┤
│  HAZARD    │                                            │ ⚠ PRIORITY     │
│  (collapse │                                            │                │
│   after    │                MAP                         │ 1 Dorzong      │
│   run)     │                                            │   13 people    │
│            │  ┌──────────────────────────────────────┐  │   water T+20   │
│  ▸ inputs  │  │ Sat│Terrain│Streets│Dark   [Layers▾] │  │   road  T+35   │
│            │  └──────────────────────────────────────┘  │   window 15m   │
│  BREACH    │                                            │ ─────────────  │
│  pess/cent │                                            │ 2 …            │
│  /opti     │  ┌──────────── scrubber ────────────────┐  │                │
│            │  │ BREACH ──●───────────────── +60m     │  │ ALERTS         │
│  EXPOSURE  │  │        ▲ T+30      ● isolation marks │  │ [CAP preview]  │
│  counters  │  └──────────────────────────────────────┘  │ [SMS preview]  │
└────────────┴────────────────────────────────────────────┴────────────────┘
```

### 17.2 Basemap switcher

| Option | Source | Note |
|---|---|---|
| **Terrain** (default) | Copernicus GLO-30 → Terrain-RGB → `hillshade` + `color-relief` | The AOI's most explanatory layer, and the DEM is already local. **Default because this is a mountain valley.** |
| **Satellite** | Esri World Imagery (free with a free ArcGIS developer account, non-revenue use, attribution to Esri **and** all data providers) | Answers "where is this?" instantly. **Verify the licence terms against a government submission before demoing** |
| **Streets** | Protomaps PMTiles AOI extract | Key-less, self-hosted, offline-capable |
| **Hybrid** | Satellite + PMTiles labels/roads on top | Best single view for an operator |
| **Dark GIS** | Current desaturated OSM | Keep as an option, stop making it the only one |

**Preserve the India external-boundary overlay.** The existing `_addIndiaBoundaryLayer()`
and the reasoning behind it (2021 geospatial guidelines; raster tiles bake in a disputed-line
rendering that can only be overdrawn) are correct and must be carried onto every basemap
option, including satellite.

### 17.3 Layer stack (bottom → top)

| # | Layer | Type | Notes |
|---|---|---|---|
| 1 | Basemap | raster / vector | switchable |
| 2 | Hillshade | `hillshade` on `raster-dem` | always on, low opacity |
| 3 | India boundary | line | always on |
| 4 | Rivers | line | **data already on disk, never drawn** |
| 5 | **Ensemble envelope** | fill, hatched | pessimistic-arm extent behind the central arm |
| 6 | **Depth** | `color-relief` on Terrain-RGB depth | continuous ramp, opacity-scrubbed |
| 7 | **Arrival-time isochrones** | line | toggle; 15/30/45/60 min contours |
| 8 | **Road status** | line, categorical | **the new layer** |
| 9 | Buildings | fill-extrusion or fill | coloured by flooded/not |
| 10 | Villages | circle + label | sized by population, coloured by priority |
| 11 | Facilities | symbol | zero here — show the empty legend entry honestly |

### 17.4 Road status encoding

Categorical, and encoded twice so it survives colour blindness:

| Status | Colour | Second channel | Meaning |
|---|---|---|---|
| Open | neutral grey | solid, thin | never cut in this run |
| At risk | amber | solid, medium | cut in the **pessimistic** arm only |
| Cut soon | orange | **dashed** | cut later in this run; label the minute |
| **Impassable** | red | **solid, thick** | depth ≥ threshold at the current scrubber time |
| No data | outline only | **dotted** | no OSM coverage — never inferred |

Threshold exposed as a UI control: **0.20 m** (2025 literature) / **0.30 m** (current
`THRESH_CAR`) / **0.50 m** (`THRESH_TRUCK`), each with its citation on hover.

### 17.5 Rules the UI must enforce

1. Never render `null`. Uncomputed → an explicit dash plus a "not computed" tooltip that
   says *why* (outside flood / no road in graph / not sampled).
2. Every panel shows its provenance badge, and the badge is **read from the data**, never
   hardcoded in HTML. Every current `COMPUTED LIVE` string in `index.html` is hardcoded.
3. Cell size and snapshot interval visible in the header at all times.
4. No solver badge without a wired solver.
5. Colour is never the only channel for a categorical distinction.

---

## 18. Recommended animation system

**Primary view is static.** The arrival-time surface answers "where is it going" in one frame.
Animation is the secondary, confirmatory view.

**Scrubber:**
- Axis in **minutes**, not frame indices.
- Cross-fade opacity between adjacent depth rasters — reads as motion instead of flicker.
- **Isolation event markers on the track**, so the operator sees when decisions happen before
  pressing play.
- Play at ~2 s per 5-minute step (2.5× slower than the current 600 ms), with speed control.
  A 30-frame animation running in 8 seconds is unreadable.
- Keyboard: ←/→ step, space play/pause. Operators use keyboards under stress.

**Wave front:** draw the leading edge as a distinct line with a 2–3 frame trailing ghost, so
direction is legible in a screenshot.

**Loading:** replace the serial 13-fetch loop (D15) with `Promise.all` over frame metadata,
and let the COG protocol byte-range-fetch pixels on demand rather than pre-loading everything.

**Do not build:** particle flow fields, 3D water surfaces, camera fly-throughs. They read as
demo polish, cost real time, and answer no operational question.

---

## 19. Recommended alert architecture

```
ranked results (provenance-stamped rows)
        ↓
   ALERT BUILDER
   · severity from priority_score + evacuation_window
   · REFUSES to emit any field whose provenance is NOT_AVAILABLE   ← the hard rule
   · templated per audience, per language
        ↓
   ┌────────┬──────────────┬───────────────┬──────────────┐
   │ CAP    │ Responder    │ Public SMS    │ Dashboard    │
   │ JSON   │ brief        │ preview       │ alert panel  │
   │ (file) │ (file/panel) │ (rendered,    │ (live)       │
   │        │              │  NOT sent)    │              │
   └────────┴──────────────┴───────────────┴──────────────┘
```

Severity mapping `[D]`:

| Severity | Rule |
|---|---|
| **EXTREME** | evacuation window < 15 min **and** population > 0 |
| **SEVERE** | window < 60 min, or water arrives < 30 min |
| **MODERATE** | inundated, window ≥ 60 min |
| **ADVISORY** | not inundated but a road link is cut |
| **NO DATA** | isolation not computed — *emitted as such*, never omitted and never estimated |

Languages: English + Hindi minimum; Ladakhi/Urdu for the Zanskar AOI. Static templates.

**Nothing is transmitted.** Keep the existing "structured for SACHET ingestion — NOT
transmitted" label and extend it to every new channel.

---

## 20. Validation strategy

### 20.1 Keep and extend what exists

The five asserted benchmarks in `tests/test_swe_validation.py` (lake-at-rest velocity 0.0 m/s,
free-surface drift 0.0 m, mass closure < 1 %, Ritter RMSE 0.043 m, front error 10 %) are the
strongest evidence in the repository. **Keep every assertion. Add to them.**

### 20.2 New validation

| Target | Method | Pass criterion |
|---|---|---|
| **SPH vs FV** | Run both on Ritter, compare against the analytical solution | Both within a stated tolerance; **plot real output**, never `Math.sin` |
| Mass conservation, real terrain | `mass_balance()`, already implemented | < 5 % unaccounted |
| Grid convergence | Same scenario at `coarsen` 1/2/4/6, compare extent and arrival | **Quantifies exactly what `coarsen: 6` costs.** This number belongs in the README, not in a comment |
| Arrival-time surface | Cross-check per-cell arrival against M6's per-village arrival | Must agree at village centroids by construction |
| Road cut correctness | Unit test: synthetic depth raster, known graph, assert exact cut set and cut times | Deterministic |
| **M5/M6 sampling agreement** | Assert no row has `inundated: true` with `water_arrival_min: null` | **Currently fails** — Dorzong is exactly this row |
| Historical extent | Compare max extent against NDEM / Bhuvan flood layers for Phutkal 2015 or Rishiganga 2021 | Report CSI/POD/FAR **as measured, including if poor** |
| Provenance lattice | Property test: derived value never carries a stronger label than its weakest input | Must hold |
| **No fabricated data** | Grep-based test failing the build on `Math.sin`/`Math.random` in a render path, and on any hardcoded metric | Prevents D1/D2 recurring |

The last row is worth writing as an actual test. D1 and D2 are the kind of defect that gets
re-added under deadline pressure; a failing test is cheaper than a judge finding it.

### 20.3 Honest reporting

If the historical comparison is poor, publish the number. The project's entire competitive
position is that it reports what it measures. A measured CSI of 0.4 with an explanation beats
an unmeasured claim of accuracy — and it is far more defensible under questioning.

---

## 21. Performance considerations

### 21.1 Measured today

| Path | Grid | Wall time | Source |
|---|---|---|---|
| Full res, `coarsen: 1` | 1189 × 1134 = 1.35 M | > 10 min end-to-end | API comment `[V]` |
| `coarsen: 4` | ~74 k | **653 s** | API comment `[V]` |
| `coarsen: 6` (live default) | ~37 k | "well under a minute" (claimed) | API comment |

The `coarsen: 4` figure at 653 s is the alarming one: 74 k cells should not take 11 minutes.
That suggests the bottleneck may be per-timestep Python overhead rather than raw cell count.
**Profile before optimising** — the fix might be vectorisation, not a coarser grid.

### 21.2 Optimisation ladder (stop at the first rung that holds)

1. **Profile.** Establish whether time is in the solver kernel, the raster I/O, or the OSMnx
   graph operations. Everything below is guesswork until this is done.
2. **Cheap wins first.** Write COGs once instead of GeoTIFF-then-GeoJSON. Drop the vectoriser
   entirely. Cache the projected road graph and edge midpoints across timesteps (M6 currently
   copies the graph every step: `G_cut = G_proj.copy()` inside the loop — 13 full graph copies).
3. **Numba/Cython on the solver kernel** if the profile points there.
4. **HAND precompute** — move terrain work out of the per-run path.
5. **GPU** (CuPy/JAX) only if 1–4 are exhausted. This is a rewrite, not a tweak.

**Target:** `coarsen: 2` (≈56 m) live in under 60 s. That is a 4× resolution improvement over
the current live path and is likely reachable on rungs 1–3 alone.

### 21.3 Frontend

| Concern | Now | Target |
|---|---|---|
| Frame load | 13 serial fetches | COG byte-range on demand |
| Depth payload | GeoJSON polygons | COG tiles, GPU-decoded |
| Basemap | External OSM tiles (rate-limited, network-dependent) | Local PMTiles — also makes offline demo possible |
| Road layer | none | few hundred features — trivial |
| Library | MapLibre 4.1.3 via unpkg CDN | ≥ 5.6, **vendored locally** — a CDN failure during judging kills the demo |

Vendoring MapLibre, Plotly and the fonts is a five-minute change that removes a total demo
failure mode. Do it early.

---

## 22. Risks and limitations

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | **A judge finds the fabricated Ritter curves (D1) or road counter (D2)** | **Fatal** | Delete both immediately. The project's whole pitch is provenance; being caught fabricating under a `COMPUTED LIVE` badge is unrecoverable |
| R2 | **Solver badges for unwired solvers (D3)** | **Fatal** | Remove the badges, or wire `sph_swe.py`. The README already gives the instruction; follow it |
| R3 | **No version control (D17)** | High | `git init` today. A lost or unbisectable regression before 20 Sep is a self-inflicted wound |
| R4 | 30 m DEM cannot support street-level claims | High | Never promise street-level. §12 is the script |
| R5 | Demo AOI has 14 hamlets, 79 buildings, 0 facilities — isolation rarely fires | High | Add a populated scenario (§8.2). Config work, biggest demo payoff |
| R6 | `coarsen: 6` (167 m) undisclosed | High | Show cell size in the header; report grid convergence |
| R7 | PS deliverable (i) SPH + Delft3D + comparison unmet | High | Wire `sph_swe.py` on Ritter. Honest partial ≫ fabricated complete |
| R8 | PS deliverable (iv) GEE near-real-time unmet | Medium | Scope it as hazard *discovery* on a 6-day revisit and say so |
| R9 | Esri World Imagery licence terms in a government submission | Medium | Verify before demoing; PMTiles + terrain is the fallback with no licence question |
| R10 | MapLibre 4→5 upgrade breaks the existing style | Medium | Route B (`maplibre-cog-protocol`, works on 4.5+) is the fallback |
| R11 | COG EPSG:3857 requirement adds a reprojection stage | Low | One `rasterio.warp` at the output boundary; internals stay UTM |
| R12 | Scope creep into ML | **High** | §7.3: ship zero new neural networks before the deadline |
| R13 | Stale README/About claims (D13, S1 revisit) | Medium | One documentation pass. Cheap, and it protects the document's credibility |
| R14 | SAR flood detection is hardest in steep terrain | Medium | If M1 is built, demo it on a flat reach, not Zanskar |

---

## 23. Keep / Replace / Upgrade / Add / Do Not Build

### KEEP — already good, do not touch

- The 2D SWE solver and its five asserted benchmarks. **This is the best thing in the repo.**
- The breach ensemble (Froehlich/Von Thun/MacDonald).
- The provenance lattice in `provenance.py` and `worst()`.
- The backend's discipline of leaving uncomputed values null.
- The India external-boundary overlay and the reasoning behind it.
- DEM choice (Copernicus GLO-30) and population choice (GHS-POP 3 arc-sec).
- Positioning against RBSD / C-FLOOD — the research doc's competitive analysis holds.
- MapLibre as the client library.
- The CAP "structured, not transmitted" honesty.

### REPLACE — remove and rebuild

- `_depth_to_geojson()` + `snapshots/*.geojson` → **COG rasters**.
- The `charts.js` fake solver curves (D1) → **real output, or an empty state**.
- The `val-roads` fabricated counter (D2) → **the real cut count from M6**.
- Hardcoded `COMPUTED LIVE` strings in `index.html` → **badges read from the data**.
- Min-max `priority_score` (D16) → **absolute scale**, comparable across runs.
- The 890 m village discs → **real polygons where available**; keep discs clearly labelled as
  proxies where not.

### UPGRADE — improve what exists

- MapLibre 4.1.3 → ≥ 5.6, vendored locally.
- Single-arm routing → **all three breach arms**, rendered as an envelope.
- 5-min snapshots → 60 s for the road timeline.
- `coarsen: 6` → target 2, with grid convergence published.
- Binary road cut → three-state passability with a graded threshold control.
- M5/M6 sampling → one shared rule.
- Animation → cross-fade, event markers, keyboard control.
- README → correct the Sentinel-1 revisit figure and the About-tab data claims.

### ADD — genuinely missing

- **Road status layer** ← the single highest-value addition in this document.
- **Arrival-time raster + isochrones** ← highest value per unit of effort.
- Basemap switcher (satellite / terrain / streets / hybrid / dark).
- Hillshade from the DEM already on disk.
- Rivers layer (data already on disk).
- Buildings layer (data already on disk).
- Alert panel with responder/public/CAP previews.
- HAND + flow accumulation.
- Wired `sph_swe.py` comparison on Ritter.
- A populated demo scenario with a real road network.
- `git init`, plus a test that fails the build on fabricated render data.

### DO NOT BUILD — impressive-looking, wrong

- **Flood probability percentages.** One deterministic run cannot produce "82 % in 2 hours".
- **ConvLSTM / PredRNN / 3D-CNN / neural operators / PINNs.** No training data, no validation
  target, no capability the physics lacks. PINNs additionally fail on shocks.
- **SAM / SAM2** for water segmentation. Wrong inductive bias.
- **Street-level urban depth.** Needs 1 m LiDAR and drainage data that do not exist here.
- **Rainfall-driven forecasting.** Different problem; Google Flood Hub already covers India.
- **A T−2h "past" segment on the scrubber.** There is no observed past. It would be invented.
- **Live Delft3D.** Known high-risk failure; precomputed is the honest option.
- **3D water surfaces, particle fields, fly-throughs.** No operational question answered.
- **Cesium / Kepler.gl / GeoServer / PostGIS** at current data volumes.

---

## 24. Phased implementation plan

Sequenced so that **integrity is fixed before features**, and every phase leaves a demoable
system.

### Phase 0 — Integrity and safety (do first, ~half a day)

1. `git init`, first commit, `.gitignore` for `data/scenarios/*`.
2. Delete the fake Ritter curve generator (D1).
3. Delete the fabricated road counter (D2).
4. Remove unwired solver badges (D3) from header and Solver Comparison tab.
5. Fix `showIsolationCallout` to use `fmtWin`/`fmtMin` (D4).
6. Fix the null-coercion in `_checkIsolationAtFrame` (D5).
7. Delete the dead demo paths: `renderDemoHydrograph`, the `onTimeSlider` fallback (D10, D11).
8. Correct README (Sentinel-1 revisit) and the About tab (D13).

**Exit:** nothing on screen that the pipeline did not compute. This phase is almost entirely
deletion, and it is the one phase that cannot be skipped.

### Phase 1 — Render what already exists (the biggest visible jump)

1. Emit `roads_timeline.geojson` from M6 — per-link `cut_time_min`.
2. Add the road status layer with the five-state encoding.
3. Compute and emit `arrival_time.cog`; add isochrone contours.
4. Add rivers and buildings layers from data already on disk.
5. Add hillshade from the DEM already on disk.
6. Reconcile M5/M6 sampling; add the assertion test.
7. Show cell size and snapshot interval in the header.

**Exit:** "which roads are affected?" and "where is it moving?" both answerable on the map.

### Phase 2 — Map quality

1. Upgrade MapLibre to ≥ 5.6, vendored locally.
2. Depth as COG + `color-relief`; delete the vectoriser and the GeoJSON snapshots.
3. Basemap switcher + PMTiles AOI extract.
4. Redesign the depth ramp for both themes; fix the ramp hole (D8) and the hardcoded
   outline colour (D9).
5. Animation: cross-fade, minute axis, isolation markers, keyboard control.
6. Rework the layout to §17.1.

**Exit:** an operator can read the map in seconds.

### Phase 3 — Analysis depth

1. Route all three breach arms; render the extent envelope.
2. Absolute priority scale (D16); recolour villages meaningfully.
3. Alert panel: CAP + responder brief + SMS preview, multilingual.
4. Wire `sph_swe.py` on the Ritter benchmark; plot **real** curves in the comparison tab.
5. HAND + flow accumulation; grid-convergence study; profile and raise the live resolution.
6. Add the populated demo scenario.

**Exit:** PS deliverable (i) honestly partial rather than fabricated; uncertainty visible.

### Phase 4 — Only if Phases 0–3 are complete and stable

1. Sentinel-1 ingest + U-Net water segmentation for M1 hazard discovery, demonstrated on a
   flat reach.
2. Prithvi-EO-2.0 clear-sky cross-check.
3. FwDET depth-from-extent, `PROXY_DATA`.
4. SFINCS as a third comparison arm.
5. Historical validation against NDEM/Bhuvan layers.

**Do not start Phase 4 before Phase 1 is done.** A satellite arm on a map that still cannot
show a cut road is effort spent in the wrong place.

---

## 25. Concrete next steps

### Immediate (today)

1. **`git init` and commit.** Nothing else should happen on an unversioned tree.
2. **Delete D1 and D2.** ~20 lines. Highest risk-reduction per keystroke in the project.
3. **Decide the badge question:** either remove the ANUGA/SWE-SPH/Delft3D badges, or commit
   to wiring `sph_swe.py` in Phase 3. Do not leave them as-is.

### This week

4. Emit `roads_timeline.geojson` and render it. This is the difference between claiming a
   differentiator and showing one.
5. Compute the arrival-time raster — one reduction over data already on disk.
6. Fix the null-rendering bugs (D4, D5) so the screenshot stops saying `null min`.
7. Run the grid-convergence study and publish what `coarsen: 6` costs.

### Open verification items — things I could not confirm

| Item | Why it matters | How to close it |
|---|---|---|
| **DeepINDRA methodology** | Government-catalogued Indian street-scale flood system = prior art the judges may know. The ISTI page refused connection (`ECONNREFUSED 164.100.228.160:443`) | Retry the ISTI portal; search the publishing institution's repository directly |
| **Inunda licence/repo** | If open, a differentiable GPU SWE solver is genuinely interesting for calibration | Read the full arXiv paper, not the abstract |
| Esri World Imagery terms for a govt submission | Basemap choice depends on it | Read the ArcGIS developer terms |
| SFINCS in **super-critical** flow | Determines whether it is a valid comparison arm for a gorge breach | Read the [SFINCS validation preprint](https://egusphere.copernicus.org/preprints/2025/egusphere-2025-4387/egusphere-2025-4387.pdf) |
| `coarsen: 4` = 653 s anomaly | 74 k cells should not take 11 minutes; the fix may be profiling, not coarsening | Profile a run |
| Projects marked `search` in §4 | Cited on description, not inspection | Open each before depending on it |

### The decision to make before any code changes

**Does the team accept that this is a dam-break consequence engine and not a flood
detection/prediction system?**

If yes, this plan applies: fix integrity, render what is computed, keep the physics, add no
neural networks before the deadline.

If the team instead wants a general flood detection and prediction system, that is a
**different project** — different data (Sentinel-1 time series), different validation
(historical events), different competitive position (against Google Flood Hub, which already
covers all of India with a 7-day lead time). It should be decided deliberately, not drifted
into by adding models to a simulator.

**Recommendation `[D]`: stay a consequence engine.** The unmapped-impoundment gap identified
in `you-are-a-senior-lucky-harp.md` is real, RBSD and C-FLOOD do not fill it, and the
time-varying road-graph cut is a genuine differentiator that no benchmarked system in §13
provides. It is currently just invisible. **Make it visible.**

---

## Sources

**External sources fetched or searched during this research.** Inspection depth as marked
in §4.

- [DeepINDRA — ISTI Portal, Government of India](https://www.indiascienceandtechnology.gov.in/research/deepindra-experimental-system-forecasting-street-scale-flood-inundation-coupling-physical-and-deep) *(listing verified; detail page unreachable)*
- [Explainable Flood Segmentation on Sentinel-1 SAR: CNN vs Transformer — arXiv 2606.16302](https://arxiv.org/abs/2606.16302)
- [Prithvi-EO-2.0 — arXiv 2412.02732](https://arxiv.org/pdf/2412.02732) · [Prithvi-EO-2.0-300M-TL-Sen1Floods11 — Hugging Face](https://huggingface.co/ibm-nasa-geospatial/Prithvi-EO-2.0-300M-TL-Sen1Floods11) · [Prithvi-EO-1.0-100M-sen1floods11](https://huggingface.co/ibm-nasa-geospatial/Prithvi-EO-1.0-100M-sen1floods11)
- [Land cover and flood type govern detection limits of satellite flood mapping — arXiv 2606.07780](https://arxiv.org/pdf/2606.07780)
- [DeepSARFlood — ScienceDirect](https://www.sciencedirect.com/science/article/pii/S2666017225000094)
- [Flood-LDM — arXiv 2511.14033](https://arxiv.org/pdf/2511.14033)
- [Inunda: GPU-Native Differentiable Flood Solver — arXiv 2607.09614](https://arxiv.org/html/2607.09614)
- [Deltares/SFINCS — GitHub](https://github.com/Deltares/SFINCS) · [SFINCS validation preprint — EGUsphere 2025](https://egusphere.copernicus.org/preprints/2025/egusphere-2025-4387/egusphere-2025-4387.pdf)
- [NOAA-OWP/inundation-mapping — GitHub](https://github.com/NOAA-OWP/inundation-mapping) · [NOAA-OWP/ras2fim](https://github.com/NOAA-OWP/ras2fim) · [sdmlua/FIMserv](https://github.com/sdmlua/FIMserv)
- [csdms-contrib/fwdet — GitHub](https://github.com/csdms-contrib/fwdet) · [FwDET v2.0 — NHESS 19, 2053 (2019)](https://nhess.copernicus.org/articles/19/2053/2019/) · [Rapid flood depth from EOS-04 — NHESS 25, 2455 (2025)](https://nhess.copernicus.org/articles/25/2455/2025/)
- [Google: open-sourcing the hydrology framework](https://research.google/blog/the-next-chapter-in-flood-resilience-open-sourcing-googles-hydrology-framework/) · [Global AI flood forecasts](https://research.google/blog/using-ai-to-expand-global-access-to-reliable-flood-forecasts/) · [Advanced Flood Hub features](https://blog.google/innovation-and-ai/products/advanced-flood-hub-features-for-aid-organizations-and-governments/)
- [ECMWF Code4Earth 2026 Challenge 11 — flood forecasting dashboard](https://github.com/ECMWFCode4Earth/Challenges_2026/issues/6)
- [geomatico/maplibre-cog-protocol](https://github.com/geomatico/maplibre-cog-protocol) · [opengeos/maplibre-gl-time-slider](https://github.com/opengeos/maplibre-gl-time-slider) · [opengeos/maplibre-gl-raster](https://github.com/opengeos/maplibre-gl-raster)
- [MapLibre Style Spec — layer types](https://maplibre.org/maplibre-style-spec/layers/) · [color-relief example](https://maplibre.org/maplibre-gl-js/docs/examples/color-relief/) · [MapLibre Newsletter June 2025](https://maplibre.org/news/2025-07-02-maplibre-newsletter-june-2025/)
- [Protomaps / PMTiles](https://protomaps.com/api) · [PMTiles for MapLibre GL](https://docs.protomaps.com/pmtiles/maplibre)
- [Sentinel-1 Mission — ASF HyP3](https://hyp3-docs.asf.alaska.edu/sentinel1/) · [Sentinel-1 — Copernicus Data Space](https://documentation.dataspace.copernicus.eu/Data/SentinelMissions/Sentinel1.html) · [ESA Sentinel-1 facts and figures](https://www.esa.int/Applications/Observing_the_Earth/Copernicus/Sentinel-1/Facts_and_figures)
- [Vertical accuracy of global DEMs (FABDEM, Copernicus, NASADEM, AW3D30, SRTM) — Int. J. Digital Earth 17(1), 2024](https://www.tandfonline.com/doi/full/10.1080/17538947.2024.2308734) · [Global evaluation of radar DEMs — JGR Biogeosciences 2024](https://agupubs.onlinelibrary.wiley.com/doi/full/10.1029/2023JG007672)
- [Bhuvan API — ISRO/NRSC](https://bhuvan-app1.nrsc.gov.in/api/) · [NRSC Disaster Management Services](https://www.nrsc.gov.in/nrscnew/Apps_DMS.php) · [data.gov.in rainfall catalogue](https://www.data.gov.in/catalog/rainfall)
- [STGCN for road-network inundation status — arXiv 2104.02276](https://arxiv.org/pdf/2104.02276) · [Web-based road accessibility DSS — Urban Informatics 2024](https://link.springer.com/article/10.1007/s44212-024-00040-0) · [Time-varying road vulnerability routing — ASCE JITSE 32(2)](https://ascelibrary.org/doi/10.1061/JITSE4.ISENG-2788) · [Road accessibility indices during floods, 2025](https://www.sciencedirect.com/science/article/pii/S2212420925002997) · [Road disruption and EMS access — Sci. Total Env. 2024](https://www.sciencedirect.com/science/article/pii/S0048969724072978)
- [High-resolution urban flood forecasting via hydrodynamic + multimodal DL — J. Hydrology 2026](https://www.sciencedirect.com/science/article/abs/pii/S0022169426003689) · [Physics-informed DL urban inundation — J. Hydrology 2024](https://www.sciencedirect.com/science/article/abs/pii/S0022169424013945) · [DL super-resolution + coarse-grid hydrodynamics — Eng. App. CFD 2025](https://www.tandfonline.com/doi/full/10.1080/19942060.2025.2481115) · [LSTM–U-Net hybrid — Water 18(11) 1360](https://doi.org/10.3390/w18111360) · [DL + rain-on-grid, Guwahati — J. Flood Risk Mgmt 2026](https://onlinelibrary.wiley.com/doi/10.1111/jfr3.70186)
- [Designing effective flood early warning systems — J. Flood Risk Mgmt 2025](https://onlinelibrary.wiley.com/doi/10.1111/jfr3.70145) · [NAPSG incident symbology guideline v4.0](https://www.napsgfoundation.org/wp-content/uploads/2020/03/NAPSG-Foundation-Incident-Symbol-Guideline_v4.0_03212020.pdf) · [Monitor real-time emergencies — Learn ArcGIS](https://learn.arcgis.com/en/projects/monitor-real-time-emergencies/)
- [Free basemap tiles for MapLibre](https://medium.com/@go2garret/free-basemap-tiles-for-maplibre-18374fab60cb) · [Esri — OpenStreetMap Wiki](https://wiki.openstreetmap.org/wiki/Esri)

---

*Research and planning document only. No code was modified. SIH26161 · NTRO · deadline 20 Sep 2026.*
