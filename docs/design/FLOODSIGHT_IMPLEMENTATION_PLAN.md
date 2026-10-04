# FloodSight Implementation Plan

Status: baseline recorded; remediation stages pending
Plan date: 2026-09-11
Source documents: `FLOODSIGHT_DEEP_REVIEW.md` and the approved implementation instructions in `C:\Users\chaud\.codex\attachments\5b5dd7ad-2c48-484f-b73f-487c486481c4\pasted-text.txt`

This document records the pre-edit baseline and the executable remediation sequence. It is documentation only. No production, test, or data files were changed while recording this plan. Existing dirty work remains in place.

## Phase 0 — baseline and safety

### Environment and worktree

- Recorded date: `2026-09-11`.
- Branch: `main`.
- HEAD: `d903da9c2121adcb4d0cbbf58e9d717aa6c865ef`.
- Host: Windows 11 `10.0.26200`.
- Python: `3.12.7`.
- Baseline was recorded before any production edit for the approved remediation.

`git status --short` at baseline:

```text
 M docs/findings_results.md
 M docs/memory.md
 M frontend/charts.js
 M frontend/index.html
 M frontend/map.js
 M frontend/spine.js
 M run_pipeline.py
 M src/api/main.py
 M src/data_fetcher.py
 M src/gee_satellite.py
 M src/m10_validation/__init__.py
 M src/m2_geometry/fill.py
 M src/m3_breach/cascade.py
 M src/m4_solvers/sph_swe.py
?? FLOODSIGHT_DEEP_REVIEW.md
?? src/m10_validation/compare_arrivals.py
?? tests/test_annamayya_cascade_arrivals.py
```

The status contains 14 pre-existing modified tracked files and three pre-existing untracked files: the review, `src/m10_validation/compare_arrivals.py`, and `tests/test_annamayya_cascade_arrivals.py`. These files are part of the worktree under review and must be preserved.

### Baseline verification already executed

- `pytest -q`: exit code `1` after `9.53s`; collection failed at `tests/test_annamayya_cascade_arrivals.py:10` with `ModuleNotFoundError: No module named 'src'`.
- `python -m pytest -q`: exit code `0` after `64.33s`; `72 passed`, `25 warnings`.
- Warnings observed: FastAPI `on_event` deprecation, `httpx` deprecation, and rasterio pending deprecation.

These results are the recorded baseline; they are not re-run as part of this documentation-only change.

### Entrypoints and registry

- Full simulation entrypoint: `run_pipeline.execute_full_simulation`.
- API entrypoint: `python -m uvicorn src.api.main:app --port 8000`.
- Frontend entrypoint: `frontend/index.html`.
- Current canonical backend registry: `SCENARIOS` in `src/data_fetcher.py`.
- Current registry keys: `phutkal`, `rishiganga`, `derna`, `malpasset`, `ivanovo`, `south_lhonak`, and `annamayya`.

### Artifact baseline

- `data/scenarios`: 125 scenario directories, 106 with `results.geojson`, approximately 7,481 files.
- `data/validation`: approximately 951 files.
- `data/population`: approximately 939 MB.
- No `*/manifest.json` files were found in the scanned scenario artifact tree.
- Legacy/generated artifacts are unverified and must not be deleted during remediation without reference checks and an explicit retention decision.

## Implementation guardrails

1. Preserve all baseline dirty work and inspect overlapping files before each edit.
2. Treat the canonical scenario/event registry and its geometry/provenance as the source of truth for backend, exports, and frontend layers.
3. Never fabricate historical observations, discharge, arrival, depth, exposure, or infrastructure values. Use `NOT_AVAILABLE` when evidence is missing.
4. Fail closed when geometry, connectivity, observation identity, mass balance, or run manifest validation fails.
5. A run is authoritative only when its manifest and validation state mark it valid. Impact counts, frame counts, and directory names cannot establish validity.
6. Keep observed, modeled, reconstructed, proxy, precomputed, and unknown values distinct, with source, version/hash, scenario, run, configuration, and uncertainty attached where applicable.
7. Do not claim ANUGA, PySPH, or Delft3D support unless the selected adapter is actually executed and verified.

## Phase 1 — P0 physical and data correctness (PENDING)

### 1. Canonical scenario and geometry manifest

Create a versioned, validated scenario/event record containing identity, event metadata, dam or blockage geometry, breach geometry, river geometry, CRS, DEM, hydrology, event timing, validation sources, provenance, and model configuration. Make backend execution, exports, API responses, and frontend context layers consume this record. Remove or quarantine duplicated frontend registries and hand-authored hydraulic paths after references are checked.

Add reusable gates for WGS84/projected round trips, raster row/column mapping, dam and breach placement, river snapping, upstream/downstream connectivity, and hydraulic path validity. A failed gate prevents a successful run.

### 2. Physical dam/blockage and breach source

Represent engineered dam body, crest, abutments, spillway, reservoir, and breach zone where evidence supports them; represent natural dams as blockage geometry. Define breach axis, opening, invert, side slopes, and formation in DEM CRS. Replace the Gaussian point-only source with a connected reservoir and physically defined opening. Generate frontend structure and outflow layers from validated backend geometry and results.

### 3. Continuous reservoir and breach state

Implement one state progression covering initial storage, inflow, level, overtopping, spillway discharge, breach initiation/growth, breach discharge, drawdown, and downstream propagation. Carry the final state between phases, preserve separate spillway and breach flow terms, and enforce volume/mass consistency. Persist phase transitions and event timestamps in the run manifest.

### 4. Annamayya cascade regression

Use the generalized event graph for Pincha failure, routed inflow, Annamayya reservoir state, breach, and downstream Cheyyeru propagation. Validate the configured breach location against the reviewed target, dam alignment, reservoir and river connectivity, invert and breach geometry, storage level, downstream path, and arrival ordering. Do not implement a separate Annamayya-only execution path.

### 5. Observation identity and isolation

Key observed/SAR artifacts by scenario, acquisition/event date, and spatial AOI. Validate source metadata and spatial compatibility before use. Remove cross-scenario fallback behavior; return `NOT_AVAILABLE` when no compatible observation exists. Do not generate generic dates or reuse another disaster's geometry.

### P0 verification

- Coordinate, raster-index, dam/breach, river-snap, connectivity, and hydraulic-path tests pass for every enabled scenario.
- Reservoir storage is continuous across phase transitions; spillway and breach terms are separately reported; mass closure is within a documented tolerance.
- Annamayya geometry and cascade regression checks pass, including arrival ordering.
- Mismatched scenario/date/AOI observations are rejected and unsupported observations surface as `NOT_AVAILABLE`.
- Production success and exports are blocked for failed geometry, physics, or observation gates.

## Phase 2 — P1 historical credibility and operational integrity (PENDING)

1. Build a source-registered Indian event catalog covering the reviewed Rishiganga, Wapriyang, Phuktal/Phutkal, Kosi, Kashmir Valley, Assam, and Annamayya cases. Enable only evidence-supported fields; leave unsupported metrics `NOT_AVAILABLE`.
2. Build reusable observed-versus-modeled comparisons for extent, arrival, depth, peak discharge, settlements, infrastructure, population, and satellite flood extent. Attach source, AOI, CRS/datum, acquisition, class, uncertainty, model run, and configuration to every metric.
3. Replace hardcoded Annamayya arrivals/impacts and current-looking precomputed Malpasset values with reproducible comparison jobs and source manifests. Label any retained benchmark as precomputed until a live run produces it.
4. Propagate all supported ensemble arms through hydraulics, exposure, isolation, ranking, and validation. Until that exists, label consequences central-only and remove any unsupported uncertainty band. Correct the ranking path so the intended filled isolation value is used, while preserving the documented meaning of never-isolated NaN as score zero; do not blindly replace it with a fill value.
5. Reproject exposure masks using CRS/transforms, preserve nodata, validate population alignment, and separate population and loss provenance.
6. Add bounded request validation, scenario-key validation, safe path policy, durable manifests, explicit statuses, cancellation, bounded workers, concurrency protection, deterministic valid-run selection, and partial/failed output gating.
7. Separate event time from simulation time. Generate frontend timeline/spine entries only from the run manifest and scenario event graph.

### P1 verification

- Source and comparison tests prove metric provenance and return `NOT_AVAILABLE` for unsupported fields.
- Full ensemble identity is preserved through exposure, isolation, ranking, and validation, or central-only behavior is explicit in API/UI output.
- Invalid requests and invalid/partial archives are rejected; valid-run selection depends on manifest validation and hashes.
- Historical event and simulation clocks remain distinct and all displayed timestamps are manifest-backed.

## Phase 3 — compound-event generalization (PENDING)

Implement a reusable event graph supporting single failure, sequential and parallel hazards, cascading failures, multiple upstream failures feeding one river, reservoir plus rainfall, natural blockage outburst, and multiple reservoirs. Nodes and edges carry state, timing, configuration, provenance, and uncertainty. Annamayya is the reference cascade, not a special case.

Verification requires Pincha-only, Annamayya-only, Pincha-to-Annamayya, multiple-upstream, and mixed hazard scenarios to execute through the same graph with deterministic phase ordering and preserved provenance.

## Phase 4 — natural-dam and lake formation (PENDING)

Derive filling and water levels from terrain/storage relationships and verified blockage/outlet geometry. Remove arbitrary stage fractions and fabricated water bodies. Validate terrain confinement, impoundment volume, surface elevation, blockage height, outlet, breach path, and downstream connectivity. Invalid natural-dam geometry remains unavailable rather than becoming a successful run.

## Phase 5 — durable API and run integrity (PENDING)

Persist deterministic run identity, input/config/data hashes, manifest schema/version, status transitions, validation results, cancellation state, and output inventory. Move CPU work to bounded workers or a bounded process executor with durable state and atomic transitions. Enforce trusted path boundaries and reject incomplete, failed, unknown, or physically invalid archives. Never use population, villages, roads, frame counts, or arbitrary impact scores to choose an authoritative run.

## Phase 6 — map performance (PENDING)

After correctness gates pass, change the map to metadata-first loading, selected-frame/on-demand snapshots, bounded LRU raster caching, viewport clipping, simplification or tiling, and server-side preprocessing where appropriate. Instrument first paint, first frame response, frame-switch p50/p95, payload size, active layers, heap after repeated switching, CPU, and playback FPS. Compare measurements with agreed budgets; do not infer compliance from code inspection.

## Phase 7 — cleanup (PENDING)

After P0–P2 behavior is working, inspect references before removing or quarantining dead scenario implementations, duplicate cascade paths, misleading solver wrappers, stale demo hydrographs, hardcoded frontend registries, stale diagnostics, and unverified generated artifacts. Parameterize diagnostics by manifest/run ID, expose component failures instead of broad swallowing, and generate README/UI scenario lists from the canonical registry. Preserve artifacts until retention and provenance decisions are recorded.

## Phase 8 — test pyramid (PENDING)

Add or repair unit tests for coordinates, raster indexing, breach geometry, stage-storage, hydrographs, snapping, and exposure intersection; integration tests for scenario-to-DEM-to-geometry through API/frontend contracts; physics tests for mass, continuity, discharge, connectivity, stability, arrivals, positivity, and CFL constraints; regression tests for Annamayya, SAR isolation, population alignment, run selection, multipart exports, and full collection; and scenario tests for Pincha, Annamayya, their cascade, natural blockage, GLOF, multiple upstream events, and analyst-defined breaches.

## Phase 9 — actual-system validation (PENDING)

Run full collection, targeted P0/P1 regressions, an Annamayya reconstruction, and at least one additional Indian scenario. Verify geometry numerically and visually, event timeline, observed-versus-modeled comparisons, performance instrumentation, generated outputs, and provenance. Do not declare completion from tests alone if physical outputs remain implausible.

## Known interpretation corrections to preserve during implementation

- FastAPI `BackgroundTasks` uses a threadpool for synchronous callables; the material issue is unbounded in-process CPU/I/O jobs, process-local state, and lack of durable scheduling, cancellation, and concurrency controls.
- The central ensemble arm is selected by sorted routed peak discharge; document that selection rather than implying a different ordering rule.
- The added spillway discharge is currently an empirical metadata adjustment and is not evidence that physical spillway flow has been double counted. The implementation must still keep rated spillway and breach terms separate and prove continuity before combining them.
- The original snapped breach `bx/by` values used raster `rowcol` correctly. Do not “fix” that mapping without evidence; validate it with explicit round-trip tests.
- SWE mass accounting already separates boundary and clipped terms. Preserve those separate terms and improve validation/reporting rather than merging them.
- `iso_for_score` is currently dead after being computed. Never-isolated NaN scoring as zero is intended behavior; only wire the existing intended value where the documented score requires it.

## Completion state

All remediation phases above are `PENDING`. The baseline is `COMPLETE`; physical, historical, geometry, API, performance, and definition-of-done gates have not yet passed. This plan makes no implementation or completion claim.
