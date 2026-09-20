# FloodSight — Physical Model & Disaster Fidelity Forensic Audit

**Date:** 2026-09-12
**Scope:** the executable code at `D:\sih_work\floodsight`, working tree as found (uncommitted changes included).
**Method:** the repository was treated as untrusted. No claim in any README, docstring, comment, prior review, plan document, `memory.md`, `findings_results.md`, or test name was accepted as evidence. Every finding below is traced from the real production call chain and, where a number is quoted, reproduced by running the code.
**Nothing was changed.** Six read-only diagnostic scripts were written to a scratchpad outside the repository; they import production modules and call them exactly as `run_pipeline.py` does.

---

## 1. Executive summary

### The answer to the central question

> **Is FloodSight actually simulating the disaster, or mostly generating plausible-looking water movement from hardcoded assumptions?**

**It is generating plausible-looking water movement.** The hydraulics inside the 2D solver are real and well-constructed. Everything that connects those hydraulics to a *disaster* — a structure, an impoundment, a failure, a release — is absent, disconnected, or hand-authored.

Six statements, each proven below with reproduced numbers:

1. **There is no dam in the terrain the solver integrates.** The barrier is raised only inside a private copy of the DEM used by the volume calculation (`fill.py:130`, `fill.py:260`). The array handed to the solver (`run_pipeline.py:1000`) has never been modified. Re-running the only valid scenario with the barrier raised in the *solver's* DEM changes the flood footprint from 195 to 185 wet cells — a 5 % difference. The impoundment is a puddle in an open valley reach, not water held back by a structure.

2. **The breach hydrograph is injected into the reservoir, not released from it.** `snap_to_thalweg` (`dem_utils.py:183`) moves the breach coordinate to the *lowest cell within 300 m*, which is by construction the deepest cell of the pool. Measured for `phutkal`: the injection cell `(148, 261)` has bed 3738.6 m and carries **58.0 m** of initial-condition water — it is the bottom of the lake. The solver then adds `Q(t)` there as a volumetric source spread over a 5×5 Gaussian kernel (`swe_2d.py:805`). Water does not leave through an opening; it appears inside the impoundment.

3. **The reservoir's water is counted twice.** The full pool is placed as `initial_depth` (`run_pipeline.py:865/883`) *and* the same pool's drainage is injected as `Q(t)`. Measured on the one run in the repository that the system itself certifies as valid (`data/scenarios/7d96e858…/manifest.json`):
   ```
   initial_m3  = 13,038,073      injected_m3 = 13,036,686
   stored_m3   = 26,089,025      outflow_m3  = 0.0
   relative_error = 2.1e-15
   ```
   **26.1 Mm³ of water for a 13.0 Mm³ impoundment — exactly 2.00×** — and the mass-balance gate scores it as perfect.

4. **Water cannot leave the computational domain.** `condition_dem` (`dem_utils.py:228`) raises every no-data cell to `max(DEM) + 100 m`. For `phutkal` that is a 6,504 m wall, and **98.1 % of the domain boundary ring is walled**; the 87 open boundary cells sit at 4,466–5,594 m while the valley floor is 3,485 m. `rishiganga`: 94.7 % walled, lowest open boundary cell 2,381 m against a 1,639 m floor. `south_lhonak`: 97.9 % walled, lowest open cell 3,899 m against a 783 m floor. `outflow_m3 = 0.000e+00` in every run measured. The flood can only accumulate; recession and downstream routing out of the AOI are not expressible.

5. **Six of seven scenarios cannot produce a valid run.** `derna`, `malpasset`, `ivanovo` fail the geometry gate at every resolution (`upstream/downstream seeds lack separated connected components`); `annamayya` fails it for missing roles; `south_lhonak` crashes on a missing geometry manifest; `rishiganga` fails the crest gate (`initial WSE 2393.0 m exceeds the DEM barrier crest 2384.13 m`). **`phutkal` is the only scenario in the repository that can complete.** All seven are offered in the UI.

6. **The breach is an input, not an output.** For every cascade scenario, `width_m`, `formation_s` and `peak_q_m3s` are typed into `data_fetcher.py`'s `SCENARIOS[...]["cascade"]["breach_ensemble"]` and read straight out at `cascade.py:411-413`. They are labelled `"source": "Froehlich (2008) …"` but **no Froehlich equation is evaluated anywhere on the cascade path**. Discharge is additionally hard-capped at `1.15 × peak_q_m3s` (`cascade.py:469`); for `south_lhonak` that cap binds exactly (computed peak 63,250 = 55,000 × 1.15).

### What is genuinely good

The 2D solver itself. Audusse hydrostatic reconstruction, MUSCL/minmod, SSP-RK2, Rusanov, Kurganov–Petrova desingularisation, point-implicit friction, and a separated mass ledger are correctly implemented and the DAMBRK weir coefficients are correctly converted to SI. The active-window optimisation is exact, not an approximation. The geometry hard gate, the crest gate, the run-manifest lifecycle and `_validate_source_identity` (which refuses every fabricated observation except Derna's real Copernicus product) are real defensive engineering that is currently doing its job — which is why almost nothing runs.

### Severity tally

| | Count |
|---|---|
| Critical | 10 |
| Major | 25 |
| Minor | 8 |

---

## 2. Real production call graph

Established by reading `src/api/main.py`, `src/api/worker.py`, `run_pipeline.py` and every module they import. Arrows are actual calls, not architecture.

```
POST /api/run                                   src/api/main.py:269
  ├─ get_scenario_manifest(scenario_key)        src/scenarios.py
  ├─ validate wse_m / duration / fill / coarsen / failure_mode   main.py:272-283
  ├─ new_manifest + write_manifest              src/run_manifest.py
  └─ subprocess.Popen(-m src.api.worker)        main.py:296
        └─ run_manifest_file()                  src/api/worker.py:10
              └─ execute_full_simulation()      run_pipeline.py:183   ← the whole model
```

`execute_full_simulation` is one 1,424-line function. Its real order of operations:

```
execute_full_simulation                                     run_pipeline.py:183
│
├─[M1] get_dem(scenario_key)                                data_fetcher.py:656
│      └─ fetch_dem → Copernicus GLO-30 tiles → UTM mosaic   data_fetcher.py:496,505
│      (generate_synthetic_dem exists but allow_synthetic=False is never overridden
│       from the API path → unreachable in production)
│
├─ compute_hydro_surfaces (HAND + flow-acc)                 dem_utils.py:282
│      ALWAYS RAISES on this environment (pysheds 0.5 calls np.in1d, removed in
│      NumPy 2.x). Caught and logged. Outputs consumed by nothing. → DEAD.
│
├─ condition_dem(raw)                                       dem_utils.py:228
│      raises no-data + low-fringe cells to max(z)+100 m  ── see C4
│
├─ dem_elev = dem_elev[::coarsen, ::coarsen]                run_pipeline.py:284
│
├─[GATE B] validate_geometry(manifest, dem, transform, crs) m2_geometry/validation.py:37
│      HARD FAIL for derna / malpasset / ivanovo / annamayya
│      FileNotFoundError for south_lhonak (no data/geometry/south_lhonak.json)
│      returns barrier_mask, upstream_seed_xy, downstream_seed_xy   (consumed)
│      returns breach_mask, upstream_basin_mask,
│              downstream_source_weights                           (CONSUMED BY NOTHING)
│
├─ snap_to_thalweg(dem, bx, by, r=300 m)                    dem_utils.py:183
│      moves the breach to the LOWEST cell in radius ── see C2
│
├─ wse_m = cascade.reservoir.z_crest_m   (cascade)          run_pipeline.py:444
│  wse_m = thalweg_z + dam_height_m      (non-cascade)      run_pipeline.py:447
│      ← the wse_m FUNCTION ARGUMENT IS OVERWRITTEN HERE, UNUSED ── see M1
│
├─[GATE 1] build_stage_storage(...)  → geometry_diagnostic  m2_geometry/fill.py:49
│      raises the barrier IN A PRIVATE COPY (fill.py:130) ── see C3
│
├─[GATE 2] reconcile_stage_storage (cascade only)           cascade.py:193
│
├─[M3] cascade path ─── simulate_reservoir_cascade × 3      cascade.py:294
│        ├─ generate_pincha_outflow   (hand-authored triangle)  cascade.py:113
│        ├─ route_muskingum_1d        (K, x from config)        cascade.py:71
│        ├─ catchment Gaussian        (typed peak/sigma)        cascade.py:369
│        ├─ _check_trigger            (z >= z_crest, both mechanisms identical) cascade.py:165
│        ├─ breach_width_at / breach_invert_at (LINEAR)         breach_kernel.py:37,42
│        └─ trapezoidal_breach_discharge, capped at 1.15·q_p_env breach_kernel.py:19 / cascade.py:469
│      → Hydrograph(central / pessimistic / optimistic)
│
│  [M3] non-cascade path ─ get_hydrographs(DamGeometry)      ensemble.py:250
│        ├─ froehlich.compute   B, t_f, Q_p                  froehlich.py:30
│        ├─ von_thun.compute    B, t_f, Q_p (= Froehlich's)  von_thun.py:47
│        ├─ macdonald.compute   B, Q_p, t_f (= Froehlich's)  macdonald.py:69
│        └─ route_breach × 3, re-labelled by routed peak     ensemble.py:148
│
├─ manning_grid  ← LULC raster if present, else elevation banding,
│                  then GHS-POP ≥1 person/cell ⇒ n = 0.08    run_pipeline.py:769-836
│
├─[STAGE F.1] initial_depth = compute_lake_depth_grids(...)  run_pipeline.py:846-891
│      cascade   : level = routed elevation at t=0  → RAISES for rishiganga & south_lhonak
│                  (level above wse_m) → caught → initial_depth = None
│      non-cascade: level = reservoir_fill × pool   → the FULL POOL ── see C1
│
├─[STAGE F.2] inflow_direction, _hydrograph_v_ms              run_pipeline.py:899-936
│
├─[M4] run_2d_swe_simulation  central arm (in-process)        swe_2d.py:595
│        pessimistic + optimistic in a ProcessPoolExecutor     run_pipeline.py:981
│        ├─ h = initial_depth;  vol_initial = h.sum()·A        swe_2d.py:701-704
│        ├─ max_h = h.copy()                                   swe_2d.py:706  ← reservoir enters max-depth
│        ├─ arrival_time = 0.0 where h ≥ 0.10 m                swe_2d.py:707  ← reservoir "arrives" at t=0
│        ├─ per step: add_h = Q(t)/A · dt · gaussian_5x5       swe_2d.py:805
│        ├─ SSP-RK2 + Rusanov + Audusse + friction             swe_2d.py:473 (_rhs)
│        └─ transmissive ghost cells, outflux ledger           swe_2d.py:585-591
│
├─ max_depth.tif, envelope.tif/.geojson, 25 depth rasters,
│  snapshots/*.geojson (+ pre-breach lake frames), previews
│
├─ sph_swe.run_scenario_thalweg_sph  → solver_comparison.json  sph_swe.py
│      returns {"available": false, …} for phutkal (measured)
│
├─ mb = sim_res.mass_closure()                                swe_2d.py:218
│
├─[M5] compute_village_exposure(inundation_raster=max_depth_tif)  exposure.py:99
├─[M6] compute_isolation_times + emit_road_cut_timeline           isolation.py:204,434
├─ arrival_time.tif  (first t with depth ≥ 0.50 m)                run_pipeline.py:1303
├─[M7] rank_villages                                              ranker.py:75
├─[M8] export_shp / export_kml / export_cap_json                  exporters.py
├─[M10] if m10.available(key): compare_extent / roads / arrivals  observed.py:120
│        available() is TRUE ONLY FOR derna (observed.py:132-141)
│        derna cannot run → M10 IS UNREACHABLE IN PRODUCTION
├─ run_ritter_benchmark()  +  ritter_dam_break_sph()               validation.py:69 / sph_swe.py
└─ validity gate → return dict                                    run_pipeline.py:1484-1606
```

### Modules that exist and are never reached from `execute_full_simulation`

| File | LOC | Status |
|---|---|---|
| `src/m4_solvers/anuga_runner.py` | 200 | imported by nothing |
| `src/m4_solvers/pysph_runner.py` | 145 | imported by nothing |
| `src/m1_ingest/national_register.py` | 126 | imported by nothing |
| `src/m3_breach/event_graph.py` | 267 | only `tests/test_event_graph.py` |
| `cascade.cascade_config_to_graph` | ~60 | only `tests/test_event_graph.py` |
| `src/m10_validation/comparison.py` | 59 | imported by nothing |
| `src/gee_satellite.py` | 150 | only `tests/test_lake_cascade_gee.py` |
| `src/m4_solvers/swe_2d_gpu.py` | 445 | reachable only via `backend="gpu"`, which nothing passes |
| `validation.run_lake_at_rest_benchmark`, `validation.mass_balance`, `validation.run_all` | — | test-only |
| `dem_utils.carve_breach_geometry` | 89 | reachable only for `annamayya` (only scenario with `breach_centerline_utm`), which fails the geometry gate first |
| `dem_utils.condition_gorge_thalweg`, `burn_channel`, `load_and_reproject` | — | no callers |
| `m10.compare_arrivals` + `HISTORICAL_ARRIVALS` | 322 | gated behind `m10.available()`, which is derna-only; `HISTORICAL_ARRIVALS` only contains `annamayya` → structurally unreachable |
| `src/observation_manifest.py` | 121 | reads `data/observations/`, which does not exist |

---

## 3. Physical data-flow graph

`terrain → structure → impoundment → inflow → storage → level → failure → breach → discharge → 2D coupling → propagation → drainage → affected areas`

| Link | What the code actually does | Classification |
|---|---|---|
| terrain | Copernicus GLO-30, reprojected to UTM, no-data raised to `max+100 m` | **observed, then modified** |
| structure / barrier | Rasterised polygon from `data/geometry/<key>.json`. Used **only** to raise a private DEM copy inside `fill.py`. **Never present in the solver's terrain.** | **absent from the physics** |
| impoundment | Seeded flood-fill below `wse_m` on the barrier-raised copy | **derived, but from terrain the solver never sees** |
| inflow (cascade) | `generate_pincha_outflow`: asymmetric triangle, peak/base/duration typed in config; + Gaussian catchment pulse, peak/σ typed in config | **synthetic, hand-authored** |
| inflow (non-cascade) | none — there is no inflow at all | **absent** |
| storage | Real forward Euler continuity `V += (Qin − Qout)·dt` (`cascade.py:492`) | **modelled** |
| water level | `elev_from_v` — analytical power law `V = α h^2.5`, or the DEM curve when Gate 2 agrees | **modelled** |
| failure initiation | `z ≥ z_crest`. Identical for both implemented mechanisms | **modelled, mechanism-blind** |
| breach development | Linear width growth to a **typed** `width_m` over a **typed** `formation_s`; linear invert erosion crest→bed | **hand-authored parameters, modelled shape** |
| discharge | DAMBRK trapezoidal weir on the routed head, **clamped to `1.15 × typed peak_q_m3s`** | **modelled then over-ridden by a typed cap** |
| 2D coupling | Scalar volumetric source over a 5×5 Gaussian at the pool's deepest cell, plus optional scalar jet momentum | **artificial injection, not a hydraulic release** |
| propagation | Well-balanced 2D SWE, second order | **modelled, sound** |
| drainage | Domain boundary is 94–98 % walled at `max(DEM)+100 m`; measured `outflow = 0.0` | **impossible** |
| affected areas | Sampled from `max_depth.tif`, which **includes the pre-existing reservoir** | **contaminated** |

**Broken links, in order of severity:** structure → impoundment (the barrier never reaches the solver); breach → 2D (injection inside the pool, and mass double-counted); propagation → drainage (closed domain); everything → validation (unreachable).

---

## 4. Defect register

Format: `Severity | Type | File:line | Current behaviour | Expected physical behaviour | Evidence | Impact | Recommended fix`

### CRITICAL

---
**C1 — The impoundment's water is placed in the domain twice**
`Critical | mass fabrication | run_pipeline.py:865, 883, 1006 + swe_2d.py:704, 805, 817`

*Current:* `initial_depth` is the complete DEM pool at `reservoir_fill × wse_m`. The same pool's drainage is then injected as `Q(t)` at the breach cell. `mass_closure()` counts `initial + injected` as `total_in`, so the sum closes perfectly.

*Expected:* the reservoir's water is one body. Either it is an initial condition that drains through an opening, or it is an injected hydrograph on a dry bed. Never both.

*Evidence:* `data/scenarios/7d96e858d7c74eefacf2b79edcff9610/manifest.json` — `initial_m3 = 13,038,073`, `injected_m3 = 13,036,686`, `stored_m3 = 26,089,025`, `relative_error = 2.1e-15`. Reproduced independently (E4): `V_ic = 1.3038e7`, `V_hydrograph = 1.3037e7`, total `2.6075e7` for a `1.3038e7 m³` impoundment = **2.00×**. Per-frame: first saved depth raster holds `1.3038e7 m³`, last holds `2.6089e7 m³`.

*Impact:* every depth, every extent, every population-at-risk number and every future CSI score for a non-cascade scenario is computed from twice the correct water volume. This is the single largest error in the model.

*Fix:* delete one of the two. The physically right answer is to keep `initial_depth` and delete the injection, replacing it with a real opening in the terrain (see C2/C3). The cheap correct answer is to keep the injection and start dry.

---
**C2 — The breach hydrograph is injected into the reservoir, not released from it**
`Critical | coupling | run_pipeline.py:436, 754 + dem_utils.py:183 + swe_2d.py:805`

*Current:* `snap_to_thalweg(..., search_radius_m=300)` moves the configured breach coordinate to the lowest DEM cell within the radius — by construction the deepest cell of the pool. The solver then adds `add_h = Q(t)/A · dt · gaussian5x5` at that cell.

*Expected:* discharge should cross a boundary between the impoundment and the downstream domain — either an opening cut through the barrier whose geometry evolves with the breach, or an inflow boundary on the downstream side of the structure.

*Evidence:* measured for `phutkal` at `coarsen=4`: `snap_to_thalweg` moved the point 272 m; the resulting cell `(148, 261)` has bed 3738.6 m — the pool floor — and carries **58.0 m of initial-condition depth**, i.e. it is at the bottom of the lake. The run's own `max_depth.tif` maximum is at exactly this cell, value 58.0 m, free surface 3796.6 m = the reservoir WSE.

*Impact:* there is no breach in the hydraulic model. The reservoir → breach → 2D chain is a closed loop: the 0-D model drains the reservoir and the 2D model puts the water back into the same reservoir.

*Secondary artefact:* because the source ignores terrain, it can stack water above the impoundment surface. 101 wet cells in the `phutkal` run have a free surface above the reservoir WSE, up to **3911.4 m (115 m of superelevation)**, including 14–16 m of water standing on cells whose beds are at or above 3796 m.

*Fix:* replace the point source with a breach-opening boundary. `validate_geometry` already returns `breach_mask` and `downstream_source_weights` for exactly this and they are currently thrown away (M12).

---
**C3 — No barrier exists in the terrain the solver integrates**
`Critical | geometry | m2_geometry/fill.py:125-140, 255-268 vs run_pipeline.py:1000`

*Current:* `build_stage_storage` and `compute_lake_depth_grids` both do `dem_array = np.where(barrier_mask & (dem < barrier_crest_m), barrier_crest_m, dem_array)` on a **local copy**. `run_pipeline.py` then hands the *unmodified* `dem_elev` to `run_2d_swe_simulation`.

*Expected:* if the pre-failure state is "water impounded behind a structure", that structure must be in the bed the solver integrates, otherwise the water is not impounded.

*Evidence (E5):* `phutkal`, `coarsen=4`. `barrier_mask` covers **4 cells**, of which 2 are below `wse+5` and would be raised (max raise 57.3 m). Running the initial pool with `Q = 0` for 1800 s:
```
SOLVER DEM (production):  wet 35 → 195 cells,  spread beyond pool 161 cells
DEM w/ barrier raised  :  wet 35 → 185 cells,  spread beyond pool 150 cells
```
Raising the barrier changes the result by 5 %. The pool escapes around it either way, because `build_stage_storage` itself reports `freeboard = 0.19 m` — the pool is at the brim of an open valley reach.

*Impact:* the "pre-failure impounded state" is not a state the 2D model can hold. The model has no dam. The gate at `run_pipeline.py:1523` that checks "is the initial WSE below the barrier crest" is checking a crest that does not exist in the solved terrain.

*Fix:* the barrier must be emplaced in `dem_elev` before the solver runs, and removed/eroded over the breach footprint as `breach_invert_at` lowers.

---
**C4 — The computational domain has no outlet**
`Critical | conservation / drainage | m2_geometry/dem_utils.py:228-270`

*Current:* `condition_dem` raises every no-data cell and every cell below `percentile(valid, 0.1) − 100 m` to `max(valid) + 100 m`.

*Expected:* a flood domain needs at least one open boundary at valley-floor level, or the model cannot express drainage, recession, or routing past the AOI.

*Evidence:* measured over all seven DEMs.

| scenario | walls | boundary ring walled | lowest **open** boundary cell | domain floor |
|---|---|---|---|---|
| phutkal | 49,243 (3.65 %) @ 6504 m | **4555 / 4642 = 98.1 %** | 4,466 m | 3,485 m |
| rishiganga | 15,014 (2.22 %) @ 6393 m | 3114 / 3290 = 94.7 % | 2,381 m | 1,639 m |
| south_lhonak | 94,313 (2.34 %) @ 8559 m | 7868 / 8038 = 97.9 % | 3,899 m | 783 m |
| derna | 18,827 @ 491 m | 3049 / 3200 = 95.3 % | 0 m | −1 m |
| malpasset | 16,995 @ 705 m | 2214 / 2294 = 96.5 % | 0 m | −1 m |
| ivanovo | 20,106 @ 478 m | 3407 / 3582 = 95.1 % | 0 m | 0 m |
| annamayya | 17,116 @ 846 m | 3877 / 4068 = 95.3 % | 0 m | 0 m |

For every scenario that can actually run (phutkal, rishiganga, south_lhonak) the lowest open boundary cell is **981 m, 742 m and 3,116 m above the valley floor respectively** — the river's exit from the AOI is walled shut. Confirmed in the raw source: 4,536 of `phutkal`'s 4,642 boundary cells are genuine `-9999` no-data. Measured `outflow_m3 = 0.000e+00` in the certified run and in all three E4 runs.

*Impact:* the flood can only accumulate. Recession, downstream arrival beyond the AOI, and any "does the flood drain when it should" question are unanswerable. `outflow_m3` is structurally zero, so the term contributes nothing to the mass gate.

*Fix:* keep the no-data wall for interior holes, but cut an explicit open outflow boundary where the flowline leaves the AOI, and account the flux through it.

---
**C5 — The mass-balance validity gate cannot detect any of C1–C4**
`Critical | false assurance | run_pipeline.py:1506-1514 + swe_2d.py:218-238`

*Current:* `residual = (initial + injected) − stored − outflow + clipped`; the run is `physics_ok` when `|residual| / total_in < 0.01`. Because `clipped` is *added back* as accounted-for mass and `initial` is counted as input, the identity is satisfied by construction for any run where water stays put.

*Expected:* a physics gate should be able to fail when mass is created, when the domain cannot drain, or when the input volume is inconsistent with the impoundment.

*Evidence:* the double-counted `phutkal` run scores `relative_error = 2.1e-15`. In E4 all three variants — production (2× water), `Q=0` (correct water), dry bed (correct water) — score `0.00000 %`. The gate has **zero discriminating power** between them. Separately, `stored_m3 (26,089,025)` exceeds `initial + injected (26,074,759)` by 14,265 m³ — mass manufactured by the positivity clip — and the `+clipped` term absorbs it silently.

*Impact:* the one number the system presents as proof of physical validity is a tautology.

*Fix:* add independent gates: (a) `injected + initial ≈ impounded volume` to within a stated tolerance; (b) `clipped / total_in` below a hard threshold as a *failure*, not an accounted term; (c) a reachable-outlet check on the conditioned DEM.

---
**C6 — Six of seven scenarios cannot produce a valid run; all seven are offered**
`Critical | scope | m2_geometry/validation.py + run_pipeline.py:1523 + frontend/index.html`

*Evidence (E1, reproduced at `coarsen=1` and `coarsen=4`):*

| scenario | outcome |
|---|---|
| phutkal | **geometry PASS, crest PASS, valid** |
| rishiganga | geometry PASS, **crest FAIL** — `initial WSE 2393.0 m exceeds the DEM barrier crest 2384.13 m` (both failed manifests in `data/scenarios/`) |
| derna | `GeometryValidationError: upstream/downstream seeds lack separated connected components` |
| malpasset | same |
| ivanovo | same |
| annamayya | `GeometryValidationError: required geometry roles unavailable: downstream_seed, river, upstream_seed` |
| south_lhonak | `FileNotFoundError` — `data/geometry/south_lhonak.json` does not exist (only `south_lhonak_chungthang.json` and `south_lhonak_moraine.json`) |

Run-directory census: 128 directories under `data/scenarios/`, 125 with no manifest, 1 completed+valid (phutkal), 2 failed (rishiganga).

*Impact:* the deliverable's headline capability — simulate these seven events — is 1/7 in practice. The UI dropdown, including `Derna dams — Libya, 2023 (Copernicus EMSR696 observed)`, offers runs that will fail.

*Fix:* either repair the geometry manifests, or remove the unrunnable scenarios from the selectable set and say why.

---
**C7 — Annamayya never fails. Breach discharge is identically zero.**
`Critical | scenario parameterisation | data_fetcher.py annamayya.cascade + cascade.py:165`

*Current:* the reservoir starts at `z_frl = 203.6 m` and must reach `z_crest = 206.0 m` to trigger.

*Evidence:* running `simulate_reservoir_cascade` on the shipped config for 20,000 s:
```
annamayya  t_trigger = None   z_max_reached = 205.524 m   Q_breach_peak = 0   V_breach = 0.000e+00
```
The reservoir peaks **0.48 m short of the crest**. Every arm of the ensemble produces `Q(t) ≡ 0`.

*Expected:* the Annamayya earthen section washed out at ~05:45 IST on 19 Nov 2021 after an upstream Pincha ring-bund washout, with reported combined discharge over 2 lakh cusecs and ten downstream villages inundated in Rajampet and Nandalur mandals.

*Impact:* the flagship Indian cascade scenario cannot reproduce its event even if the geometry gate were fixed. Note the positive reading of the same fact: the trigger *is* a genuine physical criterion and the model correctly declines to fail when forcing is insufficient (audit question 8 — **yes**). The parameterisation, not the trigger, is wrong.

*Fix:* re-derive the inflow forcing against CWC/MHA/IISc peak-inflow figures; the current `upstream.peak_q_m3s = 3964` + Gaussian catchment does not lift the pool 2.4 m.

---
**C8 — Breach width, formation time and peak discharge are configuration inputs labelled as Froehlich outputs**
`Critical | fabrication / mislabelled provenance | src/data_fetcher.py:SCENARIOS[*].cascade.breach_ensemble + cascade.py:411-413, 469`

*Current:*
```python
b_final  = float(ens_cfg["width_m"])       # cascade.py:411
t_f      = float(ens_cfg["formation_s"])   # cascade.py:412
q_p_env  = float(ens_cfg["peak_q_m3s"])    # cascade.py:413
...
q_breach_i = min(trapezoidal_breach_discharge(...), q_p_env * 1.15)   # cascade.py:469
```
The config entries carry `"classification": "MODEL RECONSTRUCTION"` and `"source": "Froehlich (2008) best estimate — …"`.

*Expected:* Froehlich's regressions take `V_w` and `h_b` and *return* `B̄`, `t_f`, `Q_p`. If a scenario cites Froehlich, the numbers should be computed from that scenario's own reservoir state.

*Evidence:* `froehlich.py` is imported only by `ensemble.py`, which is only reached on the non-cascade branch (`run_pipeline.py:734`). No cascade scenario evaluates a single Froehlich term. The typed values for e.g. `annamayya` central are `width_m: 130.0, formation_s: 1800.0, peak_q_m3s: 12200.0`; `data_fetcher.py:399` even annotates `"breach_width_m": 130.0,  # Central hydraulic width (Froehlich 2008)`.

The cap is not decorative. Measured peak vs cap:

| scenario | computed `max(q_breach)` | cap `1.15 × peak_q_m3s` | binding? |
|---|---|---|---|
| rishiganga | 19,936 | 28,750 | no |
| derna | 8,116 | 10,925 | no |
| **south_lhonak** | **63,250** | **63,250** | **yes — exactly** |
| annamayya | 0 | 14,030 | n/a (never triggers) |

*Impact:* for cascade scenarios the breach is not modelled, it is specified; and the peak discharge is, at least for South Lhonak, literally read out of the config. Audit question 10 — **typed into configuration.**

*Fix:* compute `B̄` and `t_f` from the scenario's own `V_w` and `h_b` through `froehlich.compute`, keep the typed values only as a labelled comparison band, and delete the `q_p_env` clamp (or convert it to a reported diagnostic, not a limiter).

---
**C9 — The Malpasset "benchmark" ships fabricated simulated values**
`Critical | fake benchmark | data/validation/malpasset/malpasset_benchmark.json + src/api/main.py:744 + frontend/index.html (Solvers tab)`

*Current:* a static JSON containing, for each station, both `measured_s` and `simulated_s` (and `measured_wse_m` / `simulated_wse_m` for 14 high-water marks). It is served verbatim at `GET /api/benchmarks/malpasset` and rendered in the UI as:
> `Transformer A (1.4 km)  100 s recorded vs 105 s SWE (+5.0%)`

*Evidence:* `grep -rn "malpasset_benchmark"` over the whole repository returns exactly two hits — the endpoint that reads it and the frontend that renders it. **Nothing writes it.** No code path computes a Malpasset simulated arrival time. And the `malpasset` scenario fails the geometry gate at every resolution, so it has never run.

*Impact:* the UI presents hand-typed numbers as FloodSight solver output validated against field survey. This is the most directly indefensible artefact in the repository.

*Fix:* delete the `simulated_*` fields, or delete the file and the panel, until a real Malpasset run exists.

---
**C10 — `rishiganga`'s DEM impoundment is 64,600× its configured reservoir**
`Critical | geometry / scale | run_pipeline.py:444, 480-497`

*Evidence (E1, `coarsen=1`):*
```
rishiganga   wse_m = 2393.0  (= cascade.reservoir.z_crest_m)
             DEM fill: V = 9.6919e+09 m³   A = 3.861e+07 m²   H = 753.8 m   freeboard = 0.0
             configured v_frl_mcm = 0.15  →  1.5e5 m³
```
The seeded fill at 2,393 m floods the entire upper Rishi Ganga valley to a depth of 753 m — 9.69 km³ against a configured 0.15 Mm³. The geometry manifest's own `water_level_m` is **2175.68 m**, so `validate_geometry` proves upstream/downstream separation at 2,175.68 m while all downstream physics runs at 2,393 m. The `initial_depth` block would place 9.65 km³ into the 2D domain; it is saved only by raising `ValueError: levels_m must lie within connected pool stage range` (the routed level reaches 2,399.8 m > `wse_m`), which is caught and silently degrades to a dry bed (`initial_condition_source: null` in both failed manifests).

*Impact:* two different water levels govern the gate and the physics; the confinement diagnostic and the Gate-2 stage-storage reconciliation are both computed on a 9.69 km³ object that has no physical counterpart.

*Fix:* `validate_geometry`'s `water_level_m` and the pipeline's `wse_m` must be the same number, and that number must be reconciled against the DEM before it is used.

---

### MAJOR

**M1 — `wse_m` is dead.** `run_pipeline.py:186 → 444/447`. The function argument is unconditionally overwritten before its first read. `src/api/main.py:200` accepts it, `main.py:272` validates it is finite, `frontend/map.js:2514` sends `document.getElementById("wse-input").value`. The UI control labelled **"Water level"** cannot change anything. *Fix:* remove the control, or make the argument the authority.

**M2 — `spillway_capacity_m3s` is dead.** Accepted at `main.py:208`, validated at `main.py:281`, threaded through `worker.py:28` and `run_pipeline.py:729` onto `DamGeometry` (`m3_breach/__init__.py:128`) — and read by nothing. Proven: `spillway_capacity_m3s=1.0` and `=999999.0` produce bit-identical breach parameters and routed hydrographs (E3).

**M3 — `failure_mechanism` is a label with no physics.** `_check_trigger` (`cascade.py:165-192`) returns `z >= z_crest` for both implemented mechanisms; `froehlich.py:44` and `von_thun.py:58` hardcode `k_o = 1.4`, `Z = 1.0` regardless. Proven: `OVERTOPPING_EROSION` vs `PROGRESSIVE_BREACH` give `B = 84.1 m, t_f = 0.349 h, Q_p = 11717.3, routed peak = 16323.6` — identical to every digit (E3). The code's own comments say so. Audit question 16 — **labels only.**

**M4 — Three of four accepted `failure_mode` values raise.** `failure_mode_to_mechanism` (`m3_breach/__init__.py:95`) maps `piping → PIPING` and `instantaneous → SUDDEN_RAPID_BREACH`, both `NOT_IMPLEMENTED`; `breach` raises `ValueError` by design. `require_implemented_mechanism` then raises `NotImplementedError`, which the worker converts to a failed manifest. The UI offers **Overtopping / Piping** (`index.html`, "How it fails"). Selecting Piping fails the run. On the cascade path `failure_mode` is never consulted at all.

**M5 — The three-method ensemble is not three independent methods.** Measured (E3) for `H_w = 58 m`, `V_w = 1.3038e7 m³`:
```
Froehlich  Qp = 11717.275497847028   B =  84.12   t_f = 0.34895 h
VonThun    Qp = 11717.275497847028   B = 199.90   t_f = 0.86164 h
MacDonald  Qp =  5250.553216256859   B =  16.44   t_f = 0.34895 h
Froehlich Qp == VonThun Qp  →  True
MacDonald t_f == Froehlich t_f  →  True
```
`von_thun.py:77` uses Froehlich's `Q_p` regression verbatim; `macdonald.py:87` uses Froehlich's `t_f`. Since `build_ensemble` (`ensemble.py:91`) selects pessimistic/optimistic by `sorted(arms, key=Q_p)`, two arms are tied and the ordering falls to list insertion order. The shipped `hydrograph.json` shows the consequence: `pessimistic Q_p = 11717` and `optimistic Q_p = 11717`. Breach width spans **16.4 m to 199.9 m (12×)** — that is method disagreement, not an uncertainty band.

**M6 — Froehlich's coefficient is a 1995/2008 hybrid.** `froehlich.py:44` sets `k_o = 1.4`. Froehlich (2008) specifies **`K₀ = 1.3` for overtopping**; `1.4` is Froehlich (1995a), which pairs with `0.1803` and `h_b^0.19`, not `0.27` and `h_b^0.04`. Verified against the USACE HEC-RAS 1D Technical Reference, *Estimating Breach Parameters*. Effect: `B̄` is **+7.69 %** (84.12 m vs 78.11 m at the audited case). The docstring cites "Froehlich (2008), Table 2 & Eqs 8-10", which does not support `1.4`. Classification: **modified source**.

**M7 — MacDonald & Langridge-Monopolis is cited under the wrong author and stripped of its own `t_f`.** `macdonald.py` and `m3_breach/__init__.py:16` both write "MacDonald, T. C. & Langemeier, J."; the real second author is **Langridge-Monopolis, J.** The method string `"MacdonaldLangemeier1984"` is written into `Hydrograph.method_id`. Worse, `macdonald.py:26` asserts *"t_f: not given by this method"* — M-LM **do** define `t_f = 0.0179 (V_eroded)^0.364` hours (verified, HEC-RAS reference). Classification: **incorrect citation + unsupported claim + missing equation**. `V_eroded = 0.0261 (V_w·h_w)^0.769` and `Q_p = 1.154 (V_w·h_w)^0.412` are correct.

**M8 — Von Thun & Gillette's branches are mislabelled and one is fabricated.** `von_thun.py:14-17` documents `overtopping → t_f = (B/4)/H_w`, `piping → t_f = (B/4)/(1.4 H_w)`. VT&G's actual alternative forms are `t_f = B̄/(4 h_w)` (**erosion-resistant**) and `t_f = B̄/(4 h_w + 61.0)` (**easily erodible**) — an erodibility split, not a failure-mode split. The code implements the erosion-resistant form; the "piping" form appears in no source. `B̄ = 2.5 h_w + C_b` and the four `C_b` bands are correct.

**M9 — `max_depth.tif` contains the pre-existing reservoir, and everything downstream reads it.** `swe_2d.py:706` `max_h = h.copy()` where `h = initial_depth`; `swe_2d.py:707` sets `arrival_time = 0.0` wherever the initial pool is ≥ 0.10 m. `run_pipeline.py:1274` feeds `max_depth_tif` to `compute_village_exposure`; `run_pipeline.py:1303` builds `arrival_time.tif` from the depth-raster stack, whose first frame is the reservoir; `m10.compare_extent` would score against it too. *Evidence:* in the certified `phutkal` run, 67 of 402 max-depth cells (17 %) are the reservoir, villages `Tsetang` and `Khangsar` report `max_depth_m = 58.0` — exactly the reservoir depth — and the arrival raster marks the whole pool as reached at `t = 0`.

**M10 — The validation pathway is unreachable in production.** `observed.py:132-141` `_validate_source_identity` raises for every key except `derna`, so `available()` is derna-only. `derna` fails the geometry gate. Therefore `compare_extent`, `compare_roads`, `compare_arrivals`, `agreement_geojson` and every CSI/POD/FAR number can never be produced by a run today. The `phutkal` run's `observed` block reads `{"available": false}` — correct, and also the end of the line.

**M11 — Three "observed extent" artefacts are hand-drawn.** Measured (E-obs):

| file | area | vertices | erosion test | verdict |
|---|---|---|---|---|
| `malpasset/malpasset_observed_extent.geojson` | 4.805 km² | 73 | eroding 283 m leaves **0.00 %** | a constant ~500 m wide buffered centreline |
| `gfd_dam/ivanovo_observed.geojson` f1 | 0.451 km² | **5** | — | a literal `0.007° × 0.007°` rectangle, labelled `"DFO 3896 MODIS Satellite Footprint"` |
| `gfd_dam/ivanovo_observed.geojson` f0 | 5.456 km² | 48 | — | labelled `"Dartmouth Flood Observatory DFO 3896 & Field Inundation Survey"` |
| `annamayya/annamayya_observed_extent.geojson` | 39.213 km² | 753 | eroding 662 m leaves 5.1 % | a ~1.1 km wide corridor labelled `"Sentinel-1A SAR C-Band Backscatter (< -16 dB) & Ground Survey Records"`, `"provenance": "OBSERVED"`, **no scene id, no acquisition date, no orbit** |

`data/validation/README.md` states *"Nothing here is synthetic."* That statement is false for these three files. They are currently gated off by `_validate_source_identity`; the gate is the only thing preventing them from being scored as truth. Only the Copernicus EMSR696 Derna product (1,843 polygons, activation metadata, AOI extents in `ems696.json`) is a genuine observation.

**M12 — The hydraulic coupling masks are computed and discarded.** `validate_geometry` returns `breach_mask`, `upstream_basin_mask` and `downstream_source_weights` (`validation.py:191-195`). `grep -rn` over the whole tree finds **no consumer**. The module's own docstring calls itself "Fail-closed physical geometry checks for **the hydraulic coupling boundary**" — the boundary it computes is never used to couple anything.

**M13 — `validate_geometry` compares connected-component labels across two different labellings.** `validation.py:184-186`:
```python
opened_labels, _ = ndimage.label(opened, ...)          # labelling of `valid`
if not any(opened_labels[rr, cc] == downstream_label ...)   # label id from `blocked`
```
`downstream_label` came from `ndimage.label(blocked)` at line 164. Label integers are assigned by scan order and have no correspondence between two different masks. The "breach opening is connected to downstream seed" check therefore passes or fails by coincidence. *Fix:* compare `opened_labels[downstream_rc]`, not `downstream_label`.

**M14 — The crest gate compares numbers from incompatible elevation regimes on the cascade path.** `run_pipeline.py:1523` tests `wse_m <= dem_crest_along_axis`. For cascade scenarios `wse_m = cascade.reservoir.z_crest_m`, which for `south_lhonak` is **1100 m** in a DEM whose floor is 783 m and whose actual South Lhonak lake sits near **5200 m** — the config describes the Chungthang dam, 42 km downstream and 4 km lower. The gate would pass trivially for the wrong reason. (The scenario crashes earlier on the missing manifest, so this is latent.)

**M15 — The UI displays reservoir numbers the engine does not use.** `frontend/map.js:55,90,118,148,178,206,234` hardcode e.g. `"2,450 m WSE · 70 m Barrier Height"` (rishiganga), `"3,878 m WSE"` (phutkal), `"215 m WSE"` (annamayya), and `map.js:3266` renders `Crest WSE ${dam.wse} m` / `Storage Capacity ${dam.vol} MCM`. The engine runs at **2,393 m** for rishiganga and **3,796.6 m** for phutkal; `annamayya`'s config says 206 m. For rishiganga the panel shows `Storage 15.0 MCM` while the cascade uses `0.15 MCM` and the DEM diagnostic reports `9,692 MCM` — three unreconciled numbers for one quantity.

**M16 — "checked against five benchmarks" is false.** `index.html` (Solvers tab): *"a well-balanced 2D finite-volume shallow-water solver, checked against five benchmarks … Checked on lake-at-rest, mass closure, Ritter RMSE, wave speed and front location."* In production `run_pipeline.py:1429` calls **only** `run_ritter_benchmark()`, plus an SPH Ritter comparison. `run_lake_at_rest_benchmark`, `mass_balance` and `run_all` are called only from `tests/test_swe_validation.py`.

**M17 — A third-party solver's benchmark number with no provenance.** The Solvers tab presents `Delft3D-FLOW … Ritter RMSE 0.14 m, precomputed`. FloodSight has never run Delft3D; there is no Delft3D code, input or output in the repository. It is labelled "precomputed", but the number has no source.

**M18 — 1,500+ lines of unreachable modules.** See §2 table. `anuga_runner.py` (200), `pysph_runner.py` (145), `national_register.py` (126), `event_graph.py` (267), `comparison.py` (59), `gee_satellite.py` (150), `swe_2d_gpu.py` (445, opt-in only), plus `compare_arrivals.py` (322) which is structurally unreachable because `HISTORICAL_ARRIVALS` holds only `annamayya` while `m10.available()` admits only `derna`.

**M19 — `carve_breach_geometry` is dead, and the comment chain around it is stale.** `run_pipeline.py:317-340` contains 24 lines of reasoning about ordering the geometry gate before carving. `has_explicit_breach` is true only when `breach_centerline_utm` is present, which only `annamayya` has (`data_fetcher.py:393`), and `annamayya` raises at the gate three lines earlier. The carve never executes. `tests/test_geometry_hard_gate.py:167` asserts the *source-code ordering* of two strings — it tests a textual property of dead code.

**M20 — The reservoir can rise far above its own crest.** `simulate_reservoir_cascade` has a spillway rating and a breach opening, but **no crest-overtopping weir**. Measured: `rishiganga` reaches `z = 2417.96 m` against `z_crest = 2393.0 m` — **25 m of water standing above the dam crest**. `south_lhonak` reaches 1100.99 vs 1100.0. Physically, once the level passes the crest the whole crest length discharges. *Fix:* add a crest weir over the non-breached length.

**M21 — The headline loss figure rests on one uncited constant.** `exposure.py:58` `_STRUCT_COST_INR = 300_000` and `exposure.py:54-55` a depth–damage curve `(0, 0.5, 1, 2, 3, 5) m → (0, 0.15, 0.35, 0.55, 0.75, 1.0)`. No source. `run_pipeline.py:1350` prints `Direct loss est. Rs {…}` and it is exported in `results.geojson`, `.shp`, `.kml` and the CAP alert.

**M22 — `snap_to_thalweg` structurally guarantees C2.** `dem_utils.py:183` — "Move a point to the lowest cell within `search_radius_m`". A dam's breach is on the *barrier*; the lowest cell within 300 m of a barrier on a river is the impounded thalweg behind it. The function cannot place a breach anywhere except a local minimum.

**M23 — All cascade upstream forcing comes from one hand-authored Annamayya-specific pulse.** `generate_pincha_outflow` (`cascade.py:113`) builds an asymmetric triangle with `t_peak = 0.35 × duration` and a linear recession to `2 × duration`. It is called for `rishiganga`, `derna`, `south_lhonak` and `annamayya` alike (`cascade.py:354`), with `peak_q_m3s / base_q_m3s / duration_s` typed per scenario and classified `"RECONSTRUCTION"` or `"OFFICIAL ESTIMATE"`. Catchment runoff is a Gaussian with typed `peak_m3s`, `peak_offset_s`, `sigma_s`. No hydrological model, no rainfall-runoff, no observed hydrograph. Audit question 3 — **synthetic, hand-authored.**

**M24 — `rishiganga` is modelled with the wrong physical mechanism.** The config gives the Rishi Ganga barrier a **rated spillway**: `{"cd": 1.8, "length_m": 20.0, "z_crest_m": 2392.5, "max_q_m3s": 2000.0}`. The 7 Feb 2021 Chamoli event was a ~27 × 10⁶ m³ rock-and-ice avalanche from Ronti Peak that transformed into an extraordinarily mobile debris flow; the debris-dammed lake in the Rishiganga valley formed **after** the flood (~800 m long, ~100 m wide, ~46 m deep, mapped within the following week). There was no impoundment whose failure caused the flood, and a natural avalanche barrier has no discharge coefficient or rated capacity. Modelling it as a clear-water reservoir with a spillway and a Froehlich breach is the wrong mechanism for the event, and the `MORAINE_GLOF_OUTBURST` / `NATURAL_LANDSLIDE_DAM_FAILURE` / `RIVER_BLOCKAGE_OUTBURST` enum members that would describe it are all `NOT_IMPLEMENTED`.

**M25 — The clip ledger double-counts the RK2 intermediate stage.** `swe_2d.py:836-844`: `vol_clipped` accumulates `min(h1, 0)` at the Heun predictor stage *and* `min(h_new, 0)` at the corrector. `h1` is a stage value, not a state, so its clipped mass is added to the ledger for water that was never in the solution. Effect is small (7.9e3 m³ on a 2.6e7 m³ run) but it inflates the term that the mass gate uses to absorb error.

---

### MINOR

| ID | Finding |
|---|---|
| N1 | `froehlich.py:8` documents `t_f` "[formation time, hours]" for a formula that returns seconds. The code converts correctly; only the docstring is wrong. |
| N2 | `_depth_to_geojson` (`run_pipeline.py:89`) upsamples ×4 and Gaussian-blurs with `σ = 0.9` source cells before contouring. The rendered extent is systematically larger than the computed extent. |
| N3 | Clamping `crest_length_m` **raises** the routed peak: `B = 84.1 → 50.0 m` moves the routed central peak from 16,324 to 19,600 m³/s (E3). Emergent from the drawdown coupling, but counterintuitive enough to need a stated explanation. |
| N4 | `phutkal`'s `downstream_structure` (`Phuktal Monastery`, 77.177 E) lies **east** of the breach (77.058 E) while the valley and the simulated flood both run **west** (bed 3,756 m at 77.10 → 3,669 m at 76.97). The configured "downstream" impact site is upstream. |
| N5 | `phutkal` config claims a "15 km impounded lake, 30 MCM". Contemporary reporting describes a lake ~8 km long over ~55 ha. The DEM fill gives 13.0 Mm³ / 43.6 ha at `coarsen=4` and 18.8 Mm³ / 69.4 ha at `coarsen=1`. Three numbers, none reconciled. |
| N6 | 125 of 128 directories under `data/scenarios/` have no manifest — output of a prior, pre-manifest run system, never cleaned up. |
| N7 | `src/observation_manifest.py` enforces a strict AOI/date/hash contract against `data/observations/`, a directory that does not exist. It is served at `/api/layers/{key}/{kind}` and always returns `NOT_AVAILABLE`. |
| N8 | `solver_comparison.json` for the certified run reads `{"available": false, "status": "NOT_AVAILABLE", "reason": "scenario adapter has unequal forcing, spatial support, and comparison times"}` — the "SIH26161 Deliverable i" SPH-vs-FV thalweg comparison does not produce a comparison for the only runnable scenario. |

---

## 5. Hardcoded / synthetic / fabricated-value register

| Value | Location | Nature | Verdict |
|---|---|---|---|
| `breach_ensemble[tier].width_m / formation_s / peak_q_m3s` (4 scenarios × 3 tiers = 36 numbers) | `data_fetcher.py` SCENARIOS | typed, cited as Froehlich | **unacceptable as labelled** — the citation is not honoured by any computation |
| `q_p_env * 1.15` discharge cap | `cascade.py:470` | typed limiter on a computed quantity | **unacceptable** — turns an output into an input |
| `generate_pincha_outflow` triangle shape (`t_peak = 0.35·duration`, recession to `2·duration`) | `cascade.py:124-131` | synthetic hydrograph shape, uncited | engineering approximation, must be labelled |
| `catchment_runoff` Gaussian (`base/peak/offset/sigma`) | SCENARIOS + `cascade.py:369` | synthetic | engineering approximation |
| `alpha_exp = 2.5` stage-storage exponent | SCENARIOS `reservoir` | typed power law | acceptable *if* Gate 2 reconciliation is reported; currently reported |
| `spillway {cd, length_m, z_crest_m, max_q_m3s}` on **natural** barriers (rishiganga, south_lhonak) | SCENARIOS | fabricated engineering structure | **unacceptable** — landslide/moraine dams have no rated spillway |
| `barrier_crest_m = wse_m + 5.0` | `run_pipeline.py:490, 692, 862, 880, 1143, 1158` | arbitrary 5 m freeboard used to make the fill close | engineering hack; and it never reaches the solver (C3) |
| `k_o = 1.4` | `froehlich.py:44` | wrong coefficient for the cited paper | **modified source** (M6) |
| `_C_RECT = 1.7`, `_C_SIDE = 1.35` | `ensemble.py:144-145`, `breach_kernel.py:24-25` | NWS DAMBRK in SI | **verified** — 3.1·√0.3048 = 1.712; 2.45·√0.3048 = 1.353 |
| `_CREST_WIDTH_M = 10`, `_SLOPE_UPSTREAM = 2.5`, `_SLOPE_DOWNSTREAM = 2.0` | `macdonald.py:61-63` | assumed embankment section | acceptable, is labelled |
| `_NON_ERODIBLE_FORMATION_TIME_MULTIPLIER = 4.0` | `ensemble.py:62` | engineering judgement | acceptable, is labelled |
| `_STRUCT_COST_INR = 300_000` + depth–damage curve | `exposure.py:54-58` | uncited | needs a source (M21) |
| `urban_manning_n = 0.08` from GHS-POP ≥ 1 person/cell | `run_pipeline.py:826` | proxy for land cover | acceptable, is labelled |
| Elevation-banded Manning `0.032 / 0.055 / 0.085` at thalweg+25 m / +80 m | `run_pipeline.py:781-785` | proxy | acceptable, is labelled |
| `RECONCILIATION_GAP_THRESHOLD_PCT = 25.0` | `run_pipeline.py:479` | policy threshold | acceptable, is justified |
| `malpasset_benchmark.json` `simulated_s`, `simulated_wse_m` (17 numbers) | `data/validation/malpasset/` | **fabricated results** | **unacceptable** (C9) |
| `malpasset_observed_extent.geojson` | `data/validation/malpasset/` | 73-vertex 500 m buffered line sold as LNH field survey | **unacceptable** (M11) |
| `ivanovo_observed.geojson` (2 polygons incl. a 5-vertex rectangle) | `data/validation/gfd_dam/` | sold as DFO 3896 + field survey | **unacceptable** (M11) |
| `annamayya_observed_extent.geojson` | `data/validation/annamayya/` | 1 corridor polygon, `"provenance": "OBSERVED"`, no scene id/date | **unacceptable** (M11) |
| `HISTORICAL_ARRIVALS` lat/lon/time windows, `evidence_class: "OBSERVED"` | `compare_arrivals.py:30+` | witness/police-log claims, unreachable code | needs provenance or deletion |
| `Delft3D-FLOW Ritter RMSE 0.14 m` | `frontend/index.html` | third-party number, no source | **unacceptable as displayed** (M17) |
| Per-scenario `WSE` / `Storage` strings | `frontend/map.js:55-234` | hardcoded, contradict the engine | **unacceptable** (M15) |
| `breach_ix, breach_iy = int(nx*0.18), int(ny*0.35)` | `run_pipeline.py:769` | arbitrary grid fractions | dead fallback (only when `breach_lon/lat` is None — never) |
| `volume_mcm` / `dam_height_m = 55.0` fallbacks | `run_pipeline.py:707-709` | proxy constants | fallback only, labelled `PROXY_DATA` |
| 6,504 m / 8,559 m no-data walls | `dem_utils.py:255` | terrain modification | **unacceptable at the domain boundary** (C4) |

---

## 6. Parameter causal-dependency matrix

`defined → consumed by → changes state → changes Q(t) → changes SWE → changes footprint`

| Parameter | Consumed by | Changes Q(t)? | Changes SWE? | Changes footprint? | Verdict |
|---|---|---|---|---|---|
| `wse_m` (API/UI) | nothing — overwritten `run_pipeline.py:444/447` | no | no | no | **DEAD** |
| `dam_height_m` (config, non-cascade) | `wse_m = thalweg + dam_height` → fill → `V_w` → Froehlich | yes | yes | yes | live |
| `cascade.reservoir.z_crest_m` | `wse_m`, trigger threshold, breach invert start | yes | yes (via IC) | yes | live |
| `cascade.reservoir.z_frl_m` | initial storage, spillway gate | yes | yes | yes | live |
| `cascade.reservoir.v_frl_mcm` | `alpha` of the power law; Gate 2 comparison | yes | yes | yes | live |
| `alpha_exp` | stage-storage law | yes | yes | yes | live |
| stage-storage curve (DEM) | promoted to cascade only when Gate 2 agrees | yes | yes | yes | live, conditional |
| `reservoir_fill` | `impounded_vol_m3` (non-cascade) **and** the IC pool fraction | yes | yes | yes | live (non-cascade only; no effect on cascade) |
| `upstream.peak_q_m3s / duration_s / lead_time_s` | `generate_pincha_outflow` | yes | yes | yes | live |
| `routing.k_s / x` | Muskingum | yes | yes | yes | live |
| `catchment_runoff.*` | Gaussian inflow | yes | yes | yes | live |
| `spillway.cd / length_m / z_crest_m / max_q_m3s` | reservoir outflow | yes | yes | yes | live (cascade only) |
| `spillway_capacity_m3s` (API/UI) | `DamGeometry` field, read by nothing | **no** | **no** | **no** | **DEAD** (proven E3) |
| `failure_mode` / `failure_mechanism` | trigger dispatch + `require_implemented_mechanism` | **no** (identical) | no | no | **LABEL ONLY** (proven E3); `piping`/`instantaneous`/`breach` **abort the run** |
| `dam_type` | `formation_time_h × 4` for concrete/masonry, central arm only | yes | yes | yes | live, non-cascade only, crude |
| `crest_length_m` | clamps `B̄`, scales `Q_p` | yes | yes | yes | live, non-monotonic (N3) |
| `breach_ensemble[*].width_m` | `b_final` | yes | yes | yes | live **as an input** (C8) |
| `breach_ensemble[*].formation_s` | `t_f` | yes | yes | yes | live **as an input** (C8) |
| `breach_ensemble[*].peak_q_m3s` | discharge cap | yes (when binding) | yes | yes | live **as a limiter** (C8) |
| breach invert | `breach_invert_at`, linear crest→bed | yes | yes | yes | live, but never opens the terrain |
| `manning_n` grid | friction source | n/a | yes | yes | live |
| river geometry (`build_rivers`) | breach snapping (non-explicit), SPH thalweg | indirectly | indirectly | indirectly | live, weak |
| `duration_s` | solver horizon; `max(duration, min_coverage_duration_s)` | no | yes | yes | live |
| `coarsen` | grid resolution | no | yes | yes | live (and changes gate outcomes) |
| downstream boundary condition | zero-gradient ghost cells — but the boundary is a 6.5 km wall | no | **no effect, water never reaches it** | no | **INERT** (C4) |
| `custom_dem_path` | `prepare_custom_dem` | yes | yes | yes | live |
| `lulc_raster_path` | Manning grid | no | yes | yes | live |
| `population_csv` | exposure only | no | no | no | presentation |
| `allow_synthetic` | not reachable from the API | — | — | — | CLI only |

---

## 7. Terrain / geometry audit

| Check | Result |
|---|---|
| DEM source | Copernicus GLO-30, fetched per scenario, reprojected to UTM. Matches the UI claim. |
| Vertical datum | Geometry manifests declare `EGM2008`; `validate_geometry:57-60` requires geometry and DEM datums to match. Enforced. |
| CRS | Geographic DEMs are rejected outright (`run_pipeline.py:295`). Correct. |
| Terrain modification 1 — no-data walls | `max(z)+100 m` over 1.7–5.2 % of cells, **94–98 % of the boundary ring**. Justification (stop the flood draining into resampling holes) is sound for interior holes; catastrophic at the boundary. See C4. |
| Terrain modification 2 — barrier raise | Applied to a **private copy** only. Never reaches the solver. See C3. |
| Terrain modification 3 — breach carving | `carve_breach_geometry`, reachable only for `annamayya`, which aborts first. Dead. See M19. |
| Terrain modification 4 — gorge thalweg conditioning | `condition_gorge_thalweg` has no callers. Dead. |
| Dam axis | No scenario carries a `dam_axis` distinct from the breach line. `dem_crest_along_axis` is `max(DEM)` over a ±250 m window around the breach point — i.e. the valley walls, not the structure. |
| Barrier continuity / abutment keying | Not checked anywhere. `phutkal`'s `barrier_mask` is **4 cells** at `coarsen=4` and 51 at `coarsen=1`. |
| Breach connectivity | Checked, but by comparing label ids across two different labellings (M13). |
| Upstream/downstream separation | Genuinely checked at `manifest.water_level_m`; fails for 3 scenarios. For `rishiganga` the check level (2,175.68 m) and the physics level (2,393 m) differ by 217 m (C10). |
| Pool confinement | Checked: `build_stage_storage` raises if the pool reaches the raster edge, and `run_pipeline.py:695` raises if it exceeds 25 % of the domain. `rishiganga` passes both at 38.6 km² with `freeboard = 0.0` — a "confined" basin that is the whole upper valley. |
| Flowline monotonicity | Not checked. |
| Correct physical basin | **Not established for any scenario.** `phutkal`'s fill is plausible (43.6–69.4 ha against a reported ~55 ha lake). `rishiganga`'s is not (9.69 km³). |

---

## 8. Reservoir / lake audit

The reservoir exists in **three mutually inconsistent representations** and nothing reconciles all three:

| Representation | Where | `rishiganga` | `phutkal` |
|---|---|---|---|
| Config scalar | `SCENARIOS[key]["volume_mcm"]` | 15.0 MCM | 30.0 MCM |
| Cascade 0-D state | `cascade.reservoir.v_frl_mcm`, power law `V = α h^2.5` | **0.15 MCM** | n/a (non-cascade) |
| DEM seeded fill | `build_stage_storage` | **9,692 MCM** | 13.0 MCM (`coarsen=4`) / 18.8 MCM (`coarsen=1`) |
| UI panel | `frontend/map.js` | 15.0 MCM | 30.0 MCM |

Answers to the audit's reservoir questions:

- **17. Is the reservoir spatial or 0-D?** Both, disconnected. The cascade integrator is 0-D (`V`, `z`, `elev_from_v`). `compute_lake_depth_grids` produces a spatial pool from a *different* terrain (barrier-raised). The 2D solver holds a third object with no barrier at all.
- **18. Does the DEM determine the impoundment?** Only for the diagnostic and, when Gate 2 agrees within 25 %, for the cascade's stage-storage lookup. `wse_m` itself is never DEM-derived on the cascade path.
- **22. Is water proven blocked before failure and connected after?** No. `validate_geometry` proves *topological* separation at `water_level_m` on a mask, then nothing enforces it hydraulically. E5 shows the pool spreads 161 cells in 1800 s with `Q = 0` and no failure.
- **23. Does the 2D domain contain the reservoir water?** Yes for non-cascade (as `initial_depth`), no for `rishiganga`/`south_lhonak` (the IC block raises and degrades to a dry bed — `initial_condition_source: null` in both failed manifests).
- **5. Does storage change level?** Yes. `v_res[i+1] = v_res[i] + (q_in − q_out)·dt`; `z = elev_from_v(v)`. This part is correct and continuous.
- **6. Does pre-failure state affect failure?** Yes on the cascade path (the trigger is evaluated against the routed level every step). No on the non-cascade path, where there is no reservoir state at all — `route_breach` starts at `V = V0` and drains.

---

## 9. Failure / breach-model audit

**How many breach kernels exist?** Three were written; **one is live**.

| Kernel | File | Status |
|---|---|---|
| `breach_kernel.trapezoidal_breach_discharge` + linear `breach_width_at` / `breach_invert_at` | `m3_breach/breach_kernel.py` | **live** — shared by `cascade.simulate_reservoir_cascade` and `ensemble.route_breach` |
| `event_graph.py`'s breach model | `m3_breach/event_graph.py` | dead (test-only) |
| `ensemble.make_hydrograph` triangular regression shape | `ensemble.py:101` | reachable only via `routed=False`, which nothing passes |

The kernel consolidation is real and correct — the two live paths now call the same functions with the same coefficients. That is a genuine improvement and should not be undone.

**What the breach model does and does not do:**

- Width: linear 0 → `B_final` over `t_f`. `B_final` is **typed** on the cascade path, **Froehlich-computed** on the non-cascade path.
- Invert: linear `z_crest → z_bed` over `t_f`.
- Discharge: `1.7·B·h^1.5 + 1.35·Z·h^2.5` with `h = z_res − z_invert`. DAMBRK, correctly converted.
- Side slope `Z`: `cfg.get("breach_side_slope_hv", 1.0)` — no scenario supplies one.
- **No erosion physics.** Nothing in the repository computes sediment transport, headcut migration, or shear-stress-driven widening. There is no BREACH/WinDAM/HR-BREACH-family model. The "formation time" is an input, not an outcome.
- **No mechanism differentiation.** Proven bit-identical (M3). Eleven of thirteen `FailureMechanism` members raise `NotImplementedError` — which is honest, and means the taxonomy is a declaration of intent, not a model.
- **Can a scenario correctly not fail?** **Yes** — `annamayya` demonstrates it (C7). This is the one place where the failure criterion behaves as real physics.

**Equation verification table** (against the USACE HEC-RAS 1D Technical Reference, *Estimating Breach Parameters*, and the primary citations):

| Equation | Code | Authoritative form | Verdict |
|---|---|---|---|
| Froehlich `B̄ = 0.27 k_o V_w^0.32 h_b^0.04` | `froehlich.py:47`, `k_o = 1.4` | Froehlich 2008, **`K₀ = 1.3`** overtopping | **modified source** — +7.69 % |
| Froehlich `t_f = 63.2 √(V_w/(g h_b²))` [s] | `froehlich.py:48` | identical | **verified** (docstring unit wrong, N1) |
| `Q_p = 0.607 V_w^0.295 h_w^1.24` | `froehlich.py:50`, labelled `Froehlich2008` | Froehlich **1995b** peak-outflow regression | **verified equation, mislabelled source** |
| Froehlich side slope `Z = 1.0` overtopping | `froehlich.py:45` | Froehlich 2008: 1.0 overtopping, 0.7 otherwise | **verified** (piping unimplemented) |
| VT&G `B̄ = 2.5 h_w + C_b`, `C_b ∈ {6.1, 18.3, 42.7, 54.9}` | `von_thun.py:35-44,60` | identical | **verified** |
| VT&G `t_f = B̄/(4 h_w)` | `von_thun.py:62` | VT&G **erosion-resistant** alternative | equation verified, **branch mislabelled as "overtopping"** |
| VT&G `t_f = B̄/(4·1.4·h_w)` for piping | `von_thun.py:16` (docstring only) | **appears in no source** | **fabricated** (dead) |
| VT&G `Q_p` | `von_thun.py:77` — Froehlich's regression | VT&G define no `Q_p` | substitution is disclosed, but collapses the ensemble (M5) |
| M-LM `V_eroded = 0.0261 (V_w h_w)^0.769` | `macdonald.py:81` | identical (earthfill) | **verified** |
| M-LM `Q_p = 1.154 (V_w h_w)^0.412` | `macdonald.py:80` | identical (earthfill) | **verified** |
| M-LM `t_f` | `macdonald.py:87` — Froehlich's, "not given by this method" | **M-LM give `t_f = 0.0179 V_eroded^0.364` h** | **unsupported claim + missing equation** |
| M-LM authorship | "Langemeier, J." | **Langridge-Monopolis, J.** | **incorrect citation** |
| M-LM breach side slope `0.5H:1V` | `macdonald.py:66` | identical | **verified** |
| DAMBRK weir `Q = 1.7 B h^1.5 + 1.35 Z h^2.5` | `breach_kernel.py:34` | NWS DAMBRK English `3.1 B H^1.5 + 2.45 Z H^2.5`; `3.1√0.3048 = 1.712`, `2.45√0.3048 = 1.353` | **verified** |
| Muskingum `C0/C1/C2` | `cascade.py:98-100` | standard Muskingum coefficients | **verified** |
| Ritter analytical solution | `run_pipeline.py:1441-1445` and `ritter.py` | `h = (1/9g)(2c₀ − x/t)²`, `−c₀t ≤ x ≤ 2c₀t` | **verified** |
| Audusse hydrostatic reconstruction | `swe_2d.py:473` | Audusse et al. 2004 | **verified by construction** (lake-at-rest test exists and passes, though only in the test suite) |

---

## 10. 2D coupling audit

| Question | Answer |
|---|---|
| 24. What crosses the reservoir → 2D boundary? | A scalar `Q(t)` interpolated onto the solver's timestep, plus an optional scalar jet speed. Nothing else. No stage, no water-surface elevation, no cross-section, no opening geometry. |
| 25. Scalar source, BC, or opening? | **Scalar volumetric source**, spread over a fixed 5×5 Gaussian kernel (`swe_2d.py:709`), placed at an interior cell. |
| 26. What momentum enters? | `hu += add_h · v_jet · dir_x`, `hv += add_h · v_jet · dir_y`. `v_jet = Q/(width·head)`. Cascade path uses the routed per-step `width`, `invert`, `reservoir_elevation`. **Non-cascade path uses `head = dam.height_m`, a constant** (`run_pipeline.py:935`) — the head never falls as the reservoir drains, so `v_jet` is systematically wrong in the tail. `direction` is a fixed unit vector in **grid-index space**, normalised with `hypot(d_ix, d_iy)`; correct only while `dx ≈ dy`. |
| 27. Does the domain start dry? | Non-cascade: no, it starts with the full pool (C1). `rishiganga`/`south_lhonak`: yes, because the IC block raises and is caught. |
| 28. Where does water leave? | Nowhere (C4). Transmissive ghost cells exist and are correctly implemented; the boundary is a wall. |
| 29. Is downstream drainage possible? | No, for every runnable scenario. |
| 30. Can the model create/destroy water? | Yes — `h = max(h, 0)` manufactures mass (14,265 m³ in the certified run) and the ledger absorbs it as an accounted term rather than an error. Double-counting the impoundment creates 13 Mm³ (C1). |
| 31/32. Which mass-balance function gates validity, and does the right ledger reach it? | `SimulationResult.mass_closure()` (`swe_2d.py:218`) reaches `run_pipeline.py:1509`. The old `validation.mass_balance()` is correctly retired. The ledger that arrives is the right one — it just cannot fail (C5). |

**One thing the solver gets right that is worth protecting:** the active-window optimisation is exact, not approximate, and `FLOODSIGHT_FULL_DOMAIN=1` plus `tests/test_swe_active_window.py` prove the two paths agree bit for bit. Do not touch it.

---

## 11. Conservation / numerical audit

Reproduced on `phutkal`, `coarsen=4`, 3600 s (E4):

```
                wet>0.3m       max d   initial      injected     stored       outflow   clipped    rel.err
PRODUCTION      314 (3.91 km²)  58.0 m 1.304e+07    1.303e+07    2.608e+07    0.000e+00 7.87e+03   0.00000 %
Q = 0           237 (2.95 km²)  58.0 m 1.304e+07    0.000e+00    1.305e+07    0.000e+00 8.00e+03   0.00000 %
dry bed         216 (2.69 km²)  36.1 m 0.000e+00    1.303e+07    1.311e+07    0.000e+00 7.48e+04   0.00000 %

PRODUCTION vs Q=0   : IoU = 0.7548
PRODUCTION vs dry   : IoU = 0.6879
```

Read that table carefully. **Removing the breach entirely leaves 75 % of the flood footprint unchanged.** Three-quarters of the "disaster" is the initial pool spilling out of an unconfined valley reach. The mass gate scores all three at `0.00000 %`.

Other numerical findings:

- **Timestep / stability:** CFL 0.35 with `dt ≤ 5 s`, point-implicit friction, Kurganov–Petrova desingularisation. No velocity clamp. Sound.
- **Negative depths:** clipped to zero and accounted. Magnitude is small except on the dry-bed run (7.5e4 m³, 0.57 % of injected) where the injection lands on dry cells.
- **Unit conversions:** SI throughout; geographic CRS rejected; DAMBRK coefficients correctly converted. No unit errors found.
- **Vertical datum:** declared and cross-checked between geometry manifest and DEM. But `cascade.reservoir.*` elevations are absolute MSL from agency sources with no datum declared anywhere — for `south_lhonak` they belong to a structure 4 km lower than the AOI (M14).
- **Interpolation:** `np.interp` clamps rather than extrapolates on the stage-storage curve — deliberate and correct.
- **`stored ≈ injected`:** observed and explained (C1/C4).
- **`stored > injected`:** observed (26,089,025 > 26,074,759) and explained (clipping).
- **Near-zero downstream outflow:** observed, structural (C4).
- **Water crossing terrain barriers:** observed — 101 cells with a free surface above the reservoir WSE, max +115 m, caused by the volumetric injection (C2).
- **Impossible WSE/crest relationships:** observed — `rishiganga`'s reservoir stands 25 m above its own crest (M20); and `wse_m = 2393` exceeds the DEM-sampled crest of 2384 m, which is exactly what the crest gate catches.

---

## 12. Disaster-fidelity audit

Only `phutkal` can be assessed, because it is the only scenario that completes.

**The event.** A landslide on 31 Dec 2014 dammed the Phuktal/Tsarap gorge in Zanskar; the impounded lake grew to ~8 km long over ~55 ha; an NDMA/Army diversion channel cut in April failed to drain it; the lake breached at **08:10 on 7 May 2015**, washing away three motorable and ten suspension bridges at **Ichar, Padum, Tipting, Chah and Pipcha**, two school buildings, guest houses, irrigation canals and pasture, and affecting around 40 villages in the Zanskar sub-division.

**What FloodSight produced** (`data/scenarios/7d96e858d7c74eefacf2b79edcff9610`, `coarsen=4`, 7,200 s):

| Question | Answer |
|---|---|
| 1. Does the water reach the historically affected places? | **Partially — and by accident.** The wet bounding box is lon 76.965–77.100, lat 33.237–33.327. `Ichar` **is** reported inundated, at 29.6 m depth. `Pibcha` (Pipcha) is reported **not** inundated. `Padum` (76.885 E) is outside the AOI entirely. Of 14 OSM settlements in the domain, 10 are marked inundated. |
| 2. Does the model detect the contradiction? | No. `m10.available("phutkal")` is `False`, so `observed` reports `{"available": false}` and no comparison is attempted. There is nothing to contradict. |
| 3. Does the water follow the plausible pathway? | **Yes.** Bed elevation over the wetted corridor falls monotonically westward (3,756 m at 77.10 E → 3,669 m at 76.97 E) and 256 of 302 newly-wet cells are west of the breach. The flood goes downhill down the valley. This part is right. |
| 4. Is the released volume sensible? | **No — it is exactly double.** 26.1 Mm³ released into the domain for a 13.0 Mm³ impoundment (C1). And the impoundment itself is 13.0 Mm³ against a config claim of 30 MCM and a reported lake of ~55 ha. |
| 5. Does peak discharge match credible ranges? | Unknown — no published peak for this event was found, and the model's 16,324 m³/s routed peak has nothing to be compared against. The ensemble's own spread (14,190 / 17,169 / 26,791 m³/s) comes from breach widths of 16.4–199.9 m (M5). |
| 6. Right spatial scale? | 4.59 km² at ≥ 0.3 m, of which ~17 % is the pre-existing reservoir. The real event damaged infrastructure along roughly 60 km of the Tsarap/Zanskar to Padum — outside the 32 × 31 km AOI. |
| 7. Depth distribution sensible? | **No.** Ten villages at 0.7–58.0 m. The 58.0 m figures are the reservoir. Depths of 16–56 m at riverside hamlets are not consistent with an event that washed out bridges. |
| 8. Arrival times? | Not assessable — the arrival raster marks the whole reservoir as reached at `t = 0` (M9), and there is no observed arrival dataset for this scenario. |
| 9. Does the flood recede? | **No — it cannot.** Stored volume rises monotonically from 1.30e7 to 2.61e7 m³ and outflow is identically zero (C4). |
| 11. Do inputs change the footprint causally? | Partly (see §6). But the dominant control is the initial condition, not any breach parameter: removing the entire breach changes the footprint by 25 % (§11). |
| 13. Can it distinguish a physical match from a coincidental overlap? | **No.** There is no scoring for this scenario at all, and where scoring exists (Derna) the scenario cannot run. |

**Verdict on the audit's framing question:** FloodSight produces hydraulically plausible motion in approximately the right valley, driven by the wrong amount of water released in the wrong way from a structure that is not in the terrain, into a domain that cannot drain, and it is never compared to anything. It **does not reproduce the disaster.**

---

## 13. Historical validation / provenance audit

| Dataset | Source claimed | Truly observed? | URL / activation | Date window | Coverage | CRS | Same event? | Quantitative? | Reachable? |
|---|---|---|---|---|---|---|---|---|---|
| `ems/EMSR696/*` (27 files, 1,843 polygons) | Copernicus EMS Rapid Mapping EMSR696 | **Yes** | activation metadata in `ems696.json`, code `EMSR696` verified at `observed.py:138` | 11 Sep 2023 | 6 AOIs, 74.5 km² | EPSG:4326 | Yes — Derna, Abu Mansour + Al-Bilad | Yes: CSI/POD/FAR + road damage grades + building damage points | **No — `derna` fails the geometry gate** |
| `gfd_dam/*.tif` (7 GFD/DFO rasters, incl. `DFO_3382` = Kosi 2008) | Global Flood Database / DFO | Yes | Tellman et al. 2021, CC BY-NC 4.0 | per event | 250 m MODIS | EPSG:4326 | n/a | not wired | **Not wired to any scenario** |
| `gfd_dam/ivanovo_observed.geojson` | "DFO 3896 & Field Inundation Survey" / "DFO 3896 MODIS Satellite Footprint" | **No** — 48-vertex corridor + a 5-vertex `0.007°` rectangle | none | none | 5.9 km² | CRS84 | unverifiable | would be | gated off |
| `malpasset/malpasset_observed_extent.geojson` | "LNH physical model & field survey" | **No** — 73-vertex constant-width ~500 m buffer (erodes to 0.00 % at 283 m) | none | none | 4.8 km² | CRS84 | unverifiable | would be | gated off |
| `malpasset/malpasset_benchmark.json` | "recorded transformer destruction and police survey marks" | measured column plausible; **`simulated_*` column fabricated** | none | 2 Dec 1959 | 14 marks + 3 transformers | n/a | yes for the measured half | presented as quantitative | **served live to the UI** |
| `annamayya/annamayya_observed_extent.geojson` | "Sentinel-1A SAR C-Band Backscatter (< −16 dB) & Ground Survey Records", `"provenance": "OBSERVED"` | **No** — 753-vertex ~1.1 km corridor, no scene id, no orbit, no acquisition date | none | none | 39.2 km² | CRS84 | unverifiable | would be | gated off |
| `HISTORICAL_ARRIVALS["annamayya"]` | "Field witness reports / Local police log", "AP Disaster Management / Media Surveys" | unverifiable — no document, no archive link | none | 19 Nov 2021 | 3+ points | WGS84 | plausibly | MAE would be computed | **unreachable** |
| `data/satellite/*_sentinel1_sar.geojson` | SAR-sounding metadata | **No** — ~1 KB single-polygon AOI boxes | none | none | AOI-sized | CRS84 | no | no | frontend layer only |

**Does a mismatch cause a visible failure or silent success?** Neither, today — because no comparison ever runs. The gate at `observed.py:132-141` is the right shape and is working. But the artefacts it rejects are still on disk, still referenced in `SOURCES` with `provenance = PRECOMPUTED` and `classification: "OBSERVED"`, and `data/validation/README.md` still asserts "Nothing here is synthetic."

---

## 14. Scientific literature / source verification

See the table in §9. Summary by classification:

- **verified source (7):** Froehlich 2008 `t_f`; Froehlich 2008 side slope; VT&G `B̄` and `C_b`; M-LM `V_eroded`; M-LM `Q_p`; M-LM side slope; DAMBRK weir coefficients in SI; Muskingum coefficients; Ritter.
- **verified equation, mislabelled source (2):** `Q_p = 0.607 V_w^0.295 h_w^1.24` is Froehlich 1995b, labelled 2008. VT&G `t_f = B̄/(4h_w)` is the erosion-resistant branch, labelled "overtopping".
- **modified source (1):** Froehlich `B̄` with `k_o = 1.4` (1995a) against the 2008 coefficient set.
- **unsupported (2):** "M-LM does not define `t_f`" (it does); VT&G piping `t_f` branch (invented).
- **incorrect (1):** "MacDonald & Langemeier" — the author is Langridge-Monopolis.
- **unknown (3):** `_STRUCT_COST_INR` and its depth–damage curve; the `generate_pincha_outflow` pulse shape; the `alpha_exp = 2.5` stage-storage exponent for each specific reservoir.
- **no source exists in code (1):** the mechanism taxonomy's eleven `NOT_IMPLEMENTED` members correctly raise rather than borrow overtopping physics. That is the right call and should stay.

Reference consulted in-session: USACE HEC-RAS 1D Technical Reference Manual, *Performing a Dam Break Study with HEC-RAS → Estimating Dam Breach Parameters → Estimating Breach Parameters* (fetched 2026-09-12). It reproduces all three regression sets with coefficients and units, and is the authority for M6, M7 and M8. Primary papers (Froehlich 2008 JHE 134(12):1708; MacDonald & Langridge-Monopolis 1984 JHE 110(5):567; Von Thun & Gillette 1990 USBR) were **not** fetched in full — they are paywalled — so the verification rests on the USACE secondary source, which is itself authoritative for engineering practice. Flagged as such.

---

## 15. Indian problem-statement scenario scope

The supplied problem statement names six Indian events. Current coverage:

| Named event | Repository scenario | Status |
|---|---|---|
| **Rishi Ganga, Uttarakhand — Feb 2021** | `rishiganga` | exists; **fails the crest gate at coarsen 1, 2, 4**; modelled with the wrong mechanism (M24) |
| **Wapriyang river — Nov 2021** | **none** | no scenario. Web search returns no event of this name; the closest Nov 2021 Indian dam event is the Annamayya/Cheyyeru failure (19 Nov 2021). **UNKNOWN** — the name needs clarification from the problem-statement author before anything is built. |
| **Phuktal river near Sumdo — Mar 2015** | `phutkal` | exists; **the only scenario that runs.** Note: the blockage formed 31 Dec 2014 and breached **7 May 2015**, not March — the scenario's undated "(2015)" label is compatible; a March date is not. |
| **Kosi river — 2008** | **none** | no scenario. `data/validation/gfd_dam/DFO_3382_From_20080922_to_20080929.tif` is already on disk — the shortest path to a second real observation. |
| **Kashmir Valley — 2014** | **none** | no scenario, no data |
| **Assam — 2014** | **none** | no scenario, no data |

**Four of six named events have no scenario at all. Of the two that do, one cannot run.**

Scenarios present that are **outside** the named Indian scope:

| Scenario | Country | Recommendation |
|---|---|---|
| `derna` | Libya | **Keep, relabel.** It is the only scenario with a certifiable observation (EMSR696) and is the right validation case for a two-dam cascade. It must be labelled a *validation case*, not an Indian demonstration scenario. |
| `malpasset` | France | **Keep as a numerical benchmark only.** Remove it from the runnable-scenario dropdown; delete the fabricated `simulated_*` values (C9). |
| `ivanovo` | Bulgaria | **Quarantine.** Its only "observation" is fabricated (M11) and it cannot run. |
| `annamayya` | India (not named) | **Keep, clearly labelled as a generalised Indian dam-break demonstration**, which is separately required by the problem statement. It is the best-documented Indian dam-break case in the repository. Currently it cannot run and cannot fail (C7). |
| `south_lhonak` | India (not named) | **Quarantine until the config is resolved.** The cascade block describes the Chungthang dam (z 1050–1100 m) while the AOI and DEM are the South Lhonak moraine lake (z ≈ 5200 m). Two geometry manifests exist under different names and neither matches `scenario_key`. |

**Do not** substitute `annamayya` or `south_lhonak` for the named events. They are legitimate Indian cases; they are not the cases the statement asked for.

---

## 16. What is genuinely working

Keep these. They are the load-bearing parts.

1. **`src/m4_solvers/swe_2d.py`'s numerics.** Well-balanced Audusse reconstruction, MUSCL/minmod, SSP-RK2, Rusanov, Kurganov–Petrova desingularisation, point-implicit Manning friction, correctly-padded transmissive ghost cells (no `np.roll` wrap), and a separated in/stored/out/clipped ledger. The lake-at-rest property is real.
2. **The active-window optimisation.** Exact, with a perimeter invariant check every step, an environment escape hatch, and a bit-for-bit equivalence test.
3. **`breach_kernel.py`.** One trapezoidal DAMBRK kernel shared by both live paths, coefficients correctly converted to SI, linear growth laws replacing the previous uncited exponents.
4. **`require_implemented_mechanism`.** Eleven unimplemented mechanisms raise instead of silently reusing overtopping physics. This is exactly right.
5. **`_validate_source_identity`.** Refuses every observation artefact whose identity cannot be verified. It is the single reason the fabricated extents are not being scored as truth.
6. **The geometry hard gate and the crest gate.** They are why six scenarios fail. That is the gates working, not the gates being wrong.
7. **The run-manifest lifecycle.** `queued → running → completed/failed`, artifact registration that demotes validity on failure, `is_valid` re-checked on every read, worker isolation in a bounded subprocess. Fails closed throughout.
8. **Provenance labelling.** `Provenance` / `worst()` propagation, `PROXY_DATA` on fallback volumes, null metrics for villages the flood never reached instead of invented ones. The mechanism is sound; the problem is what it is applied to.
9. **Reservoir continuity on the cascade path.** One continuous integration from before the upstream failure through the breach, with a per-step trigger test that can genuinely decline to fire.
10. **CRS discipline.** Geographic DEMs rejected outright; everything internal in UTM metres; one reprojection at the output boundary.

---

## 17. What is fake, disconnected, or dead

**Fake (presented as something it is not):**
- `malpasset_benchmark.json`'s `simulated_*` values, served live to the Solvers tab (C9)
- `malpasset_observed_extent.geojson`, `ivanovo_observed.geojson`, `annamayya_observed_extent.geojson` (M11)
- `data/satellite/*_sentinel1_sar.geojson`
- `data/validation/README.md`'s "Nothing here is synthetic"
- "checked against five benchmarks" (M16)
- "Delft3D-FLOW Ritter RMSE 0.14 m" (M17)
- Every `"source": "Froehlich (2008) …"` string on a typed breach parameter (C8)
- Frontend WSE / Storage panels (M15)

**Disconnected (computed, never used):**
- `breach_mask`, `upstream_basin_mask`, `downstream_source_weights` (M12)
- `wse_m` API parameter (M1)
- `spillway_capacity_m3s` (M2)
- `failure_mechanism` as physics (M3)
- HAND and flow-accumulation surfaces (raise every run, consumed by nothing)
- `geometry_diagnostic` for `rishiganga` — computed on a 9.69 km³ object (C10)
- The barrier, from the solver's point of view (C3)
- The downstream boundary condition, behind a 6.5 km wall (C4)

**Dead (unreachable):**
- `anuga_runner.py`, `pysph_runner.py`, `national_register.py`, `event_graph.py`, `cascade_config_to_graph`, `m10_validation/comparison.py`, `gee_satellite.py`
- `dem_utils.carve_breach_geometry`, `condition_gorge_thalweg`, `burn_channel`, `load_and_reproject`
- `validation.run_lake_at_rest_benchmark`, `validation.mass_balance`, `validation.run_all`
- `ensemble.make_hydrograph` (triangular fallback)
- `m10.compare_arrivals` + `HISTORICAL_ARRIVALS`
- `observation_manifest.validated_observation` (reads a directory that does not exist)
- `swe_2d_gpu.py` (opt-in only, nothing opts in)
- The entire M10 validation path, because `derna` cannot run

---

## 18. What must be deleted

1. `data/validation/malpasset/malpasset_benchmark.json` — or at minimum every `simulated_*` field — and the Solvers-tab panel that renders it.
2. `data/validation/malpasset/malpasset_observed_extent.geojson`, `data/validation/gfd_dam/ivanovo_observed.geojson`, `data/validation/annamayya/annamayya_observed_extent.geojson`, `data/satellite/*_sentinel1_sar.geojson`. Remove `malpasset`, `ivanovo` and `annamayya` from `observed.SOURCES` until real products exist.
3. The "five benchmarks" and "Delft3D-FLOW Ritter RMSE" claims in `frontend/index.html`.
4. The `wse_m` input and the `Piping` option from the UI, or make both real.
5. The `spillway_capacity_m3s` parameter from `RunRequest`, `worker.py`, `execute_full_simulation` and `DamGeometry`, or wire it into the reservoir routing.
6. `anuga_runner.py`, `pysph_runner.py`, `national_register.py`, `comparison.py` (~530 LOC of unreachable code).
7. `event_graph.py` + `cascade_config_to_graph` (~330 LOC) — the kernel consolidation made the migration they existed for obsolete; keeping them alive invites a future "just wire it up" that would silently swap breach physics.
8. The `q_p_env * 1.15` discharge clamp (`cascade.py:470`).
9. The 125 manifest-less directories under `data/scenarios/`.
10. `data/validation/README.md`'s "Nothing here is synthetic" sentence.
11. `tests/test_geometry_hard_gate.py:167` — a test that asserts the textual ordering of two strings inside dead code.

---

## 19. What must be rebuilt

**The impoundment→breach→2D coupling, as one thing.** The three defects C1, C2 and C3 are one defect wearing three faces: there is no structure in the solved terrain, so there is nothing for a breach to open, so the discharge has to be faked as a source, so the reservoir has to be supplied twice. Fixing them separately will not work.

Target:

```
barrier_mask ──emplace──► dem_solver (barrier at real crest elevation)
                              │
initial_depth = pool behind that SAME dem_solver, at the routed pre-failure level
                              │
0-D reservoir state (z, V) ───┼──► trigger ──► breach_invert_at / breach_width_at
                              │                      │
                              │            lower dem_solver over breach_mask
                              │            to breach_invert(t), width breach_width(t)
                              ▼                      ▼
                    the 2D solver drains the pool through the opening it just cut.
                    NO Q(t) injection. Q is an OUTPUT, measured at the opening.
```

That single change makes `Q(t)` an emergent quantity, removes the double count by construction, puts the failure mechanism somewhere it can matter, and gives the mass gate something real to check.

**Also requiring rebuild:**

- **An open outflow boundary.** Identify where the flowline leaves the AOI, leave those cells un-walled, and account the flux. Without this nothing about recession, drainage or downstream arrival is meaningful.
- **The mass-balance gate.** Add: `injected + initial ≈ impounded_volume ± tol`; `clipped/total_in < tol` as a *failure*; a reachable-outlet check.
- **`wse_m` as one number.** `validate_geometry`'s `water_level_m`, the pipeline's `wse_m`, the cascade's `z_crest_m`, `SCENARIOS[key]["wse_m"]` and the UI panel must be the same quantity or must be explicitly and visibly different quantities.
- **Cascade breach parameters from Froehlich, not from config.** Compute `B̄`, `t_f`, `Q_p` from each scenario's own `V_w`, `h_b`. Keep the typed values as a labelled comparison band.
- **Crest overtopping.** Add a weir over the non-breached crest length so the reservoir cannot stand 25 m above its own dam.
- **Non-cascade inflow.** `route_breach` starts at `V0` and drains with no inflow. A landslide dam in a live river has base flow at minimum.
- **Exposure and validation must exclude the pre-existing reservoir.** Subtract the `t=0` pool from `max_depth` before sampling exposure, arrival times, or extent skill.
- **Froehlich `k_o = 1.3`**, MacDonald's own `t_f`, correct authorship, correct VT&G branch labels.
- **`validate_geometry:185`** label-id comparison bug.

**Needs new data:**
- Geometry manifests that pass for `derna`, `malpasset`, `ivanovo`, `annamayya`; a manifest named `south_lhonak.json`.
- Real observed extents for any Indian scenario. Kosi 2008 is the cheapest (`DFO_3382` is already on disk).
- Reservoir elevations for `south_lhonak` that match the AOI, or a separate scenario for Chungthang.
- Inflow forcing for `annamayya` sufficient to reach the crest, traceable to CWC/MHA/IISc figures.

**Needs scientific research:**
- A debris-flow / hyperconcentrated-flow formulation for Chamoli-type events. Clear-water SWE with a Froehlich breach is the wrong model for a 27 Mm³ rock-ice avalanche.
- An erosion-based breach model (BREACH / WinDAM / HR BREACH family) if `formation_time` is ever to be an output rather than an input.
- A published depth–damage function for Indian rural structures to replace `_STRUCT_COST_INR`.

---

## 20. Evidence-backed implementation order

Ordered by *what unblocks the most* and *what is most indefensible if shown to a reviewer*, not by difficulty.

**Phase 0 — stop asserting things that are not true (hours, no physics risk)**
1. Delete the fabricated Malpasset `simulated_*` values and its UI panel (C9).
2. Delete or hard-quarantine the three fabricated observed extents and the SAR AOI boxes; fix the README sentence (M11).
3. Remove "five benchmarks" and the Delft3D RMSE from the Solvers tab (M16, M17).
4. Remove the `wse_m` input and the `Piping` option from the UI; remove `spillway_capacity_m3s` from the API surface (M1, M2, M4).
5. Make the UI's WSE/Storage panels read the engine's numbers, or remove them (M15).
6. Mark the six unrunnable scenarios as unavailable in the dropdown with the actual reason (C6).

**Phase 1 — make the one number that gates validity capable of failing (1 day)**
7. Add the three independent mass gates (C5).
8. Fix the RK2 clip double-count (M25).
9. Verify against the certified `phutkal` run: it must now **fail**, with `injected + initial = 2.00 × impounded`.

**Phase 2 — the coupling rebuild (the main work)**
10. Emplace the barrier in `dem_elev` before the solver (C3). Re-run E5 — the `Q=0` pool must now stay put.
11. Cut the breach opening in `dem_elev` over `breach_mask`, driven by `breach_invert_at` / `breach_width_at` (C2, M12).
12. Delete the `Q(t)` injection; measure `Q(t)` at the opening as an output (C1, C2).
13. Re-run the E4 decomposition. `PROD vs Q=0` should no longer be meaningful, because there is no `Q` to remove — and the footprint must now change materially when `breach_width_m` changes.

**Phase 3 — drainage (must follow Phase 2, or the domain fills and the comparison is meaningless)**
14. Open the outflow boundary at the flowline exit; account the flux (C4).
15. Confirm `outflow_m3 > 0` and that stored volume peaks and falls.

**Phase 4 — scenario correctness**
16. Reconcile `wse_m` / `water_level_m` / `z_crest_m` to one number per scenario (C10, M14).
17. Fix `validate_geometry:185` (M13); re-test `derna`/`malpasset`/`ivanovo` — one of them may pass once the check is correct.
18. Add the crest-overtopping weir (M20).
19. Re-parameterise `annamayya`'s inflow until it triggers, against sourced agency figures (C7).
20. Compute cascade breach parameters from Froehlich; delete the `1.15×` clamp (C8).

**Phase 5 — science and citation hygiene (independent, can run in parallel)**
21. `k_o = 1.3`; MacDonald `t_f = 0.0179 V_eroded^0.364`; fix authorship; fix VT&G branch labels; give Von Thun a `Q_p` that is not Froehlich's (M5–M8).
22. Exclude the `t=0` pool from exposure / arrival / extent (M9).
23. Source the loss model (M21).

**Phase 6 — validation**
24. Fix `derna`'s geometry so the one real observation becomes reachable (M10).
25. Wire Kosi 2008 from `DFO_3382` as the second observation and the first Indian one.
26. Only then report CSI/POD/FAR.

**Phase 7 — Indian scope**
27. Resolve "Wapriyang" with the problem-statement author.
28. Build Kosi 2008, Kashmir 2014, Assam 2014 scenarios, or state explicitly that they are out of scope for this submission.
29. Relabel `derna` / `malpasset` as validation cases, `annamayya` / `south_lhonak` as generalised Indian demonstrations.

**Do not** start at Phase 2 with the existing architecture assumed correct. Phase 1 must land first, because without a gate that can fail there is no way to know whether Phase 2 worked.

---

## 21. What FloodSight actually simulates today

*All UI and documentation claims removed. This is the executable model.*

> For **one** scenario (`phutkal` — six of seven abort before producing output):
>
> A 30 m Copernicus GLO-30 tile is reprojected to UTM and every no-data cell — including 98 % of the domain boundary — is raised to a 6,504 m wall, making the domain a closed bucket with no hydraulic outlet.
>
> A polygon from a hand-authored geometry manifest is rasterised to four cells and used to raise a **private copy** of that terrain by up to 57 m. A seeded flood-fill on that private copy, to a level set by `thalweg + 58 m`, returns a 13.0 Mm³ pool with 0.19 m of freeboard. The terrain the solver will integrate is never modified. **There is no dam in the model.**
>
> That 13.0 Mm³ pool is placed into the 2D domain as an initial condition, standing in an open valley reach that cannot hold it.
>
> Separately, a 0-D reservoir of the same 13.0 Mm³ is drained through a trapezoidal DAMBRK weir whose width comes from Froehlich's 1995/2008 hybrid regression and whose invert erodes linearly. The resulting 13.0 Mm³ hydrograph is then added back into the 2D domain as a volumetric source, spread over a 5×5 Gaussian kernel, at the cell that `snap_to_thalweg` identified as the lowest within 300 m — **the bottom of the same pool**.
>
> The domain therefore receives 26.1 Mm³ of water for a 13.0 Mm³ impoundment. The volumetric source ignores terrain and can raise the free surface 115 m above the reservoir's own water level.
>
> A well-balanced second-order 2D shallow-water solver then routes this water correctly downhill, 9 km west along the Tsarap valley, wetting 4.6 km² at ≥ 0.3 m over two simulated hours. **Removing the entire breach hydrograph changes that footprint by 25 %** — three-quarters of the flood is the initial pool spilling out of an unconfined reach. No water leaves the domain; stored volume rises monotonically to the last timestep.
>
> The maximum-depth raster includes the pre-existing reservoir, so ten OSM settlements are reported inundated at 0.7–58.0 m, two of them at exactly the reservoir's 58.0 m depth, and the arrival-time raster marks the whole pool as reached at `t = 0`. Population at risk is 2 people; every isolation time is null. A hardcoded ₹300,000 per structure and an uncited depth–damage curve produce the loss figure.
>
> A mass ledger confirms that `(initial + injected) − stored − outflow + clipped = 0` to 2 parts in 10¹⁵, and the system certifies the run as physically valid on that basis.
>
> The result is never compared to any observation, because the only verified observation in the repository belongs to a scenario that cannot run.
>
> For the four cascade scenarios, a 0-D reservoir is forced by a hand-authored triangular pulse (named for a different event) routed through Muskingum, plus a typed Gaussian catchment hydrograph. The reservoir rises, and if it crosses a configured crest elevation a breach opens with a **typed** width and **typed** formation time, discharging through the shared weir subject to a **typed** peak cap. `annamayya` never crosses its crest and produces zero discharge. `rishiganga` rises 25 m above its own crest. Neither reaches the 2D solver: `annamayya` fails the geometry gate, `rishiganga` fails the crest gate.

---

## 22. Target physical model

```
REAL GEOMETRY
  surveyed or DEM-resolved barrier footprint + crest profile + dam axis
  breach zone polygon on the barrier
  river flowline with an identified AOI exit
        │  emplaced into the SOLVER's DEM, not a copy
        ▼
IMPOUNDMENT
  seeded fill behind the emplaced barrier → V(z), A(z) from the same terrain
  reconciled against the configured stage-storage, disagreement surfaced
        │
        ▼
INFLOW
  observed hydrograph where one exists;
  rainfall-runoff or a sourced reconstruction where it does not;
  base flow always non-zero
        │
        ▼
CONTINUOUS STORAGE / LEVEL STATE
  dV/dt = Q_in − Q_spill(z) − Q_crest(z) − Q_breach(z, t)
  z = z(V) from the reconciled curve
        │
        ▼
MECHANISM-SPECIFIC FAILURE
  overtopping erosion   : z ≥ z_crest, erosion-rate-limited initiation
  piping                : gradient/seepage criterion, separate growth law
  moraine / landslide-dam outburst : mechanism-appropriate criterion
  each with its OWN initiation test and OWN growth law, or NOT_IMPLEMENTED
        │
        ▼
BREACH EVOLUTION
  width and invert from an erosion model, not from typed constants
  Froehlich / VT&G / M-LM retained as an INDEPENDENT comparison band,
  each with its own Q_p and its own t_f
        │
        ▼
HYDRAULIC BOUNDARY  ← the thing that does not exist today
  the breach opening is CUT IN THE SOLVER'S TERRAIN as it grows.
  the 2D solver drains the pool through it.
  Q(t) is MEASURED at the opening, not imposed.
        │
        ▼
2D SWE  (keep exactly what is there — it is the best part of the codebase)
        │
        ▼
DRAINAGE
  open outflow boundary at the flowline exit, flux accounted
  stored volume must peak and fall
        │
        ▼
CONSEQUENCES
  exposure sampled from (max_depth − initial_pool)
  arrival times excluding cells wet at t=0
        │
        ▼
VALIDATED COMPARISON
  identity-verified observation, same event, same date window, stated CRS
  CSI / POD / FAR / bias on extent; MAE on arrival; peak Q and released volume
  a mismatch must be visible, not absorbed
```

**Retain:** `swe_2d.py` numerics and active window; `breach_kernel.py`; `require_implemented_mechanism`; `_validate_source_identity`; the geometry and crest gates; the run-manifest lifecycle; provenance propagation; CRS discipline; cascade reservoir continuity.

**Rewrite:** the coupling (C1–C3); `condition_dem`'s boundary treatment (C4); the mass gate (C5); cascade breach parameterisation (C8); `wse_m` resolution (C10); exposure's input raster (M9).

**Delete:** §18.

**New data:** geometry manifests for five scenarios; Kosi 2008 observation; South Lhonak elevations; Annamayya inflow forcing.

**New science:** debris-flow rheology for Chamoli-type events; an erosion-based breach model; an Indian depth–damage function.

---

## 23. Appendix — file/function/line index

| Ref | Location | What |
|---|---|---|
| A1 | `src/api/main.py:268-307` | `/api/run` — validation, manifest creation, worker spawn |
| A2 | `src/api/main.py:197-210` | `RunRequest` — includes the dead `wse_m` and `spillway_capacity_m3s` |
| A3 | `src/api/worker.py:10-66` | subprocess entrypoint → `execute_full_simulation` |
| A4 | `run_pipeline.py:183-1606` | `execute_full_simulation` — the entire model |
| A5 | `run_pipeline.py:284-286` | coarsening |
| A6 | `run_pipeline.py:340-346` | geometry hard gate call site |
| A7 | `run_pipeline.py:364-380` | `dem_crest_along_axis` sampling (±250 m `max`) |
| A8 | `run_pipeline.py:382-393` | `carve_breach_geometry` — unreachable (M19) |
| A9 | `run_pipeline.py:436-437` | `snap_to_thalweg` — puts the breach at the pool bottom (C2/M22) |
| A10 | `run_pipeline.py:441-449` | **`wse_m` overwritten** (M1) |
| A11 | `run_pipeline.py:456` | `seed_xy` from the validated upstream seed |
| A12 | `run_pipeline.py:480-536` | Gate 1 confinement + Gate 2 stage-storage reconciliation |
| A13 | `run_pipeline.py:566-676` | cascade M3 branch |
| A14 | `run_pipeline.py:677-745` | non-cascade M3 branch, `DamGeometry` construction |
| A15 | `run_pipeline.py:753-770` | breach cell resolution |
| A16 | `run_pipeline.py:771-836` | Manning grid (LULC → elevation banding → GHS-POP urban override) |
| A17 | `run_pipeline.py:838-891` | **`initial_depth` — the double count** (C1) |
| A18 | `run_pipeline.py:893-936` | jet velocity and direction; non-cascade uses constant head |
| A19 | `run_pipeline.py:997-1010` | central-arm solver call |
| A20 | `run_pipeline.py:1226-1239` | mass closure logging |
| A21 | `run_pipeline.py:1484-1545` | **validity gate** (C5) |
| A22 | `src/m2_geometry/validation.py:37-198` | `validate_geometry`; **line 185 label bug** (M13); **lines 191-195 unused outputs** (M12) |
| A23 | `src/m2_geometry/fill.py:125-140` | **barrier raised in a private copy** (C3) |
| A24 | `src/m2_geometry/fill.py:255-268` | same, in `compute_lake_depth_grids` |
| A25 | `src/m2_geometry/fill.py:191-196` | edge-touch confinement check |
| A26 | `src/m2_geometry/dem_utils.py:183-226` | `snap_to_thalweg` |
| A27 | `src/m2_geometry/dem_utils.py:228-270` | **`condition_dem` — the domain walls** (C4) |
| A28 | `src/m3_breach/__init__.py:40-112` | `FailureMechanism`, `require_implemented_mechanism`, `failure_mode_to_mechanism` |
| A29 | `src/m3_breach/__init__.py:119-129` | `DamGeometry` — carries the dead `spillway_capacity_m3s` |
| A30 | `src/m3_breach/cascade.py:113-136` | `generate_pincha_outflow` — the synthetic pulse (M23) |
| A31 | `src/m3_breach/cascade.py:165-192` | `_check_trigger` — mechanisms identical (M3) |
| A32 | `src/m3_breach/cascade.py:411-413` | **typed breach parameters** (C8) |
| A33 | `src/m3_breach/cascade.py:469-472` | **`q_p_env * 1.15` cap** (C8) |
| A34 | `src/m3_breach/cascade.py:485-500` | spillway + breach per step; **no crest weir** (M20) |
| A35 | `src/m3_breach/breach_kernel.py:19-51` | the one live kernel — verified |
| A36 | `src/m3_breach/froehlich.py:44` | **`k_o = 1.4`** (M6) |
| A37 | `src/m3_breach/von_thun.py:14-17, 62, 77` | fabricated piping branch, mislabelled branch, borrowed `Q_p` (M5, M8) |
| A38 | `src/m3_breach/macdonald.py:26, 87` | false "no `t_f`" claim, borrowed `t_f` (M7) |
| A39 | `src/m3_breach/ensemble.py:91-97` | pessimistic/optimistic by `Q_p` sort — two arms tied (M5) |
| A40 | `src/m3_breach/ensemble.py:148-247` | `route_breach` |
| A41 | `src/m4_solvers/swe_2d.py:84-127` | active window — exact |
| A42 | `src/m4_solvers/swe_2d.py:218-238` | `mass_closure` (C5) |
| A43 | `src/m4_solvers/swe_2d.py:473-593` | `_rhs` — Audusse + Rusanov + outflux ledger |
| A44 | `src/m4_solvers/swe_2d.py:701-707` | initial condition; **`max_h` and `arrival_time` seeded from the pool** (M9) |
| A45 | `src/m4_solvers/swe_2d.py:709-723` | 5×5 Gaussian injection kernel (C2) |
| A46 | `src/m4_solvers/swe_2d.py:798-817` | **the injection loop** (C1, C2) |
| A47 | `src/m4_solvers/swe_2d.py:836-844` | RK2 clip double-count (M25) |
| A48 | `src/m5_exposure/exposure.py:54-58` | uncited loss model (M21) |
| A49 | `src/m10_validation/observed.py:73-118` | `SOURCES` — three fabricated entries (M11) |
| A50 | `src/m10_validation/observed.py:120-149` | `available` / `_validate_source_identity` — derna-only (M10) |
| A51 | `src/m10_validation/compare_arrivals.py:30+` | `HISTORICAL_ARRIVALS` — unreachable |
| A52 | `src/data_fetcher.py:61-495` | `SCENARIOS` — all typed scenario parameters |
| A53 | `src/data_fetcher.py:393-401` | `annamayya` `breach_centerline_utm`, `breach_width_m`, `spillway_capacity_m3s` |
| A54 | `data/validation/malpasset/malpasset_benchmark.json` | **fabricated `simulated_*`** (C9) |
| A55 | `frontend/map.js:55-234` | hardcoded per-scenario WSE/Storage (M15) |
| A56 | `frontend/map.js:2511-2522` | the run payload — sends the dead `wse_m` |
| A57 | `frontend/index.html` Solvers tab | "five benchmarks", Delft3D RMSE, Malpasset panel (M16, M17, C9) |
| A58 | `data/scenarios/7d96e858d7c74eefacf2b79edcff9610/manifest.json` | the only certified-valid run; the mass ledger that proves C1, C4, C5 |

### Diagnostic scripts used (scratchpad, outside the repository)

| ID | What it measured |
|---|---|
| E1 | Geometry gate outcome, barrier-raise magnitude, DEM fill volume and initial-condition volume for all 7 scenarios at `coarsen=1` and `coarsen=4` |
| E2 | Cascade trigger, level trajectory, peak-cap binding, IC-level validity for all 4 cascade scenarios |
| E3 | Parameter causal probe at the M3 layer — `failure_mechanism`, `dam_type`, `spillway_capacity_m3s`, `crest_length_m`, volume; ensemble-arm equation identity; `k_o` sensitivity |
| E4 | `phutkal` causal decomposition — production vs `Q=0` vs dry bed, with the full mass ledger and IoU |
| E5 | Whether the impoundment is confined in the terrain the solver integrates, with and without the barrier raised |
| E-obs | Constant-width buffer test (progressive erosion) on the three "observed extent" artefacts |
| E-wall | Boundary-ring wall census and lowest open boundary cell per scenario, against the raw DEM no-data pattern |

### Reproduction

Every number in this report was produced by importing production modules and calling them exactly as `run_pipeline.py` does, on the working tree as found on 2026-09-12. Nothing in the repository was modified. The one pre-existing artefact relied on — `data/scenarios/7d96e858d7c74eefacf2b79edcff9610/manifest.json`, written 2026-09-12T03:57:09 — was independently reproduced by E4 to within the difference expected from a 3,600 s versus 7,200 s horizon.

The test suite was run: **153 passed** in 297 s. It passes while six of seven scenarios cannot run, because several tests encode those failures as the expected outcome (`test_annamayya_raises_geometry_validation_error_before_any_artifact`) and the one end-to-end integration test uses `phutkal` specifically because it is the only scenario that works — a fact its own docstring documents at length.

---
---

# Part II — The river as a hydraulic pathway, and flow regime

**Date:** 2026-09-12 (same working tree, continued)
**Difference from Part I:** Part I was read-only. Part II ends with code changes. Every change is listed in §30 with the measurement that motivated it; every number below was reproduced by running the code.

---

## 24. The fifteen channel questions, answered from the code

| # | Question | Answer |
|---|---|---|
| 1 | Is the river explicitly represented in the hydraulic model? | **It was not.** `grep -n "channel\|width_m\|hydraulic_radius\|wetted\|conveyance\|froude\|supercritical" src/m4_solvers/swe_2d.py` returned **zero matches**. Flow direction came only from the DEM's own bed slope through the Audusse source term. |
| 2 | Where does the channel geometry come from? | `data_fetcher.build_rivers` → OSM `waterway ∈ {river, stream, riverbank}` for the scenario bbox, cached to `data/admin/<key>_rivers.geojson`. Real data, correctly fetched. |
| 3 | Did that geometry reach the SWE solver? | **No.** Tracing every use of `rivers` / `river_pts` in `run_pipeline.py`: (a) `snap_to_thalweg` seeding on the non-explicit-breach branch (lines 418–437), (b) the SPH thalweg fallback (lines 1057–1062), which returns `{"available": false}` for phutkal. Nothing else. The river never entered `run_2d_swe_simulation`. |
| 4 | Did the solver distinguish channel from floodplain? | **No.** Roughness came from elevation banding (`thalweg+25 m` / `+80 m` → 0.032 / 0.055 / 0.085) and a GHS-POP urban override. Neither knows where the channel is. In Derna the population override was setting the wadi bed itself to the built-up n = 0.08. |
| 5 | What determines flow direction at every stage? | The bed-slope source inside the well-balanced flux, `dhu -= g·h̄·dz/dx` (`swe_2d.py:577`), plus the pressure gradient. Purely local, purely DEM. No flow-direction network, no D8, no enforced drainage. |
| 6 | What determines depth, velocity, discharge, Froude? | Depth from continuity; velocity from `_desingularise(h, hu)` (Kurganov–Petrova); discharge is `h·u` across a face; **Froude is never computed anywhere** — it is not a state, a diagnostic, or a limiter. |
| 7 | Do velocity and depth respond correctly to slope, width and confinement? | **Yes — when the geometry exists.** See §25: this part of the solver is correct and was never the problem. |
| 8 | Does the wave continue downstream, or pond? | **It ponds.** See §26 for the measurement. |
| 9 | Why did the flood stop mid-valley? | Three compounding causes, all measured in §26: a sealed domain boundary, a mapped river that is a staircase of bowls in a 30 m surface model, and an escape route that existed only through cell corners. |
| 10 | Where does energy/momentum go downstream? | Into the friction sink (point-implicit Manning) and into Rusanov's numerical diffusion. Both are correct for SWE. There is no energy loss term for expansions, bends, or structures. |
| 11 | Is there an actual downstream hydraulic gradient, or do we rely on the DEM? | Entirely the DEM. And the DEM did not provide one: the required head to reach the domain boundary was **1.1–1.6 km** (§26). |
| 12 | Is river roughness differentiated from floodplain/urban? | It was not. It is now (§30, Fix 3), and §25 shows why it is the lever that matters. |
| 13 | Is supercritical flow allowed? | **Yes.** Rusanov with `a = max(\|u\|+c)` handles transcritical flow; the old 20 m/s velocity clamp was already removed in favour of point-implicit friction. Measured Froude up to **2.19** on the idealised channel and **17** on real terrain (the latter is an injection artefact, not physics — see §26). |
| 14 | Does the outburst enter the river at the correct location and geometry? | **No.** `snap_to_thalweg` places the injection at the lowest cell within 300 m, which is the pool floor, and the source is a 5×5 Gaussian volumetric kernel. This is Part I's defect **C2** and is unchanged by Part II. |
| 15 | Is discharge and momentum preserved breach → channel → floodplain? | Mass, yes (the ledger closes). Momentum, **no**: the injected jet is a scalar speed applied uniformly over the kernel, and on the non-cascade path the head it was divided by was a constant `dam.height_m` that never fell as the reservoir drained. Fixed in §30, Fix 5. |

---

## 25. Controlled experiment: the solver responds correctly to channel geometry

Idealised 61×220 grid, 20 m cells, 2 km of floodplain falling in +x with a rectangular channel incised along the centreline; Gaussian inflow hydrograph, peak 800 m³/s; state read at t = 900 s; the cross-section probe is at x = 2.4 km.

```
                                   front    wet   h_max  |v|max   at x=2.4 km
                                    (km)  cells     (m)   (m/s)   h      v      Q      Fr
baseline  s=0.010 w=100 n_ch=0.035   3.12    771    2.00    4.37  1.91  3.89   517.3  1.16
CHANNEL REMOVED (floodplain only)    1.04   1677    0.62    0.83  0.00  0.00     0.0  0.00
```

**Removing the channel cuts the front from 3.12 km to 1.04 km (−67 %), the depth from 2.00 m to 0.62 m and the speed from 4.37 m/s to 0.83 m/s, and the flow never reaches x = 2.4 km at all — while wetting more than twice as many cells.** That is the signature the user described: the water spreads, thins, slows and stops. It is exactly what FloodSight was doing on real terrain, for exactly this reason.

```
slope           front(km)  |v|max  Fr_max      width(m)   h_max  |v|max  front(km)
0.002             1.90      2.10    0.00        400        0.60   1.99     1.80
0.005             2.44      3.20    1.38        200        0.99   2.84     2.30
0.010             3.12      4.37    1.16        100        2.00   4.37     3.12
0.020             3.88      5.18    1.53         40        2.95   4.47     3.38
0.050             4.38      6.26    1.78

channel incision  Fr_max     n_channel   |v|max  front(km)  h@2.4km   n_floodplain  front(km)
 1 m               0.00       0.020       6.52     4.26      1.24       0.035          3.12
 3 m               0.98       0.035       4.37     3.12      1.91       0.080          3.12
 5 m               1.16       0.060       2.99     2.28      —          0.150          3.12
10 m               1.71       0.100       2.00     1.70      —
```

Every response is monotone and in the physically correct direction:

- **Slope** ↑ → velocity ↑, front ↑, depth ↓, Froude ↑ through the critical point. Textbook.
- **Width** ↓ → depth ↑, velocity ↑, front ↑. Correct confinement response.
- **Incision** ↑ → Froude ↑ (0 → 1.71). Confinement produces supercritical flow.
- **Channel roughness** ↑ → velocity ↓, depth ↑, front ↓. Monotone over a 5× range of n.
- **Floodplain roughness** has **no effect at all** (front 3.12 km for n = 0.035, 0.080 and 0.150) because the flow stays in the channel.

That last row is the important one. **The roughness lever that matters is the channel's, and that is precisely the one the pipeline did not have** — its grid was built from elevation bands and population density, neither of which knows where the river is.

**Conclusion: the solver was never the problem. The geometry never reached it.**

---

## 26. Why the flood stopped mid-valley — measured

### 26.1 The mapped river is a staircase of bowls

Sampled along the OSM reach that passes the breach, on the conditioned DEM the solver integrates, sampling **on the centreline**:

| scenario | dx | adverse steps | total adverse rise | net fall | largest downstream sill | points needing > 2 m of ponding |
|---|---|---|---|---|---|---|
| phutkal | 28 m | 53/119 (44.5 %) | 172.8 m | 17 m | 30.4 m | 110/120 |
| phutkal | 112 m | 12/29 (41.4 %) | 201.5 m | 28 m | 53.2 m | 24/30 |
| rishiganga | 28 m | 32/85 (37.6 %) | 184.6 m | 234 m | 40.4 m | 42/86 |
| rishiganga | 113 m | 6/20 (30.0 %) | 109.1 m | 226 m | 65.3 m | 4/21 |

A gorge floor 30–60 m wide does not resolve in a 30 m **surface** model; each sampled cell is a blend of channel and valley wall. To the solver the river is a chain of disconnected bowls separated by sills up to 65 m high. **Water must fill each bowl to its sill before it can advance**, which is the ponding.

### 26.2 The domain was sealed

The single decisive number. **Escape head** = the water level, above the release bed, at which the release cell first connects to the raster edge moving only across cell faces (a bottleneck/minimax search — the connectivity a finite-volume scheme actually has):

| scenario | dx | escape head, as found |
|---|---|---|
| phutkal | 112 m | **1572.5 m** |
| phutkal | 56 m | 1563.9 m |
| phutkal | 28 m | 1548.8 m |
| rishiganga | 113 m | 1101.5 m |
| rishiganga | 57 m | 1129.2 m |
| rishiganga | 28 m | 1116.1 m |

A flood had to stack a **one-and-a-half kilometre** column of water before a single cubic metre could leave. The domain was not "hard to drain"; it was mathematically closed. `volume_outflow_m3` was identically `0.000e+00` in every run ever measured.

### 26.3 The stall, observed directly

`phutkal`, coarsen 4, 6 hours, production forcing (Part I's C1 double-count left in place and identical in both arms):

```
 t_min  stored_Mm3  wet_km2  front_km  lon_min   max_d  |v|max
     0      13.04      0.77      2.20  77.0591    44.7   11.71
    30      25.57      2.89      6.87  77.0098    36.5   67.76
    60      26.09      3.76     10.43  76.9730    30.0   67.42
    90      26.09      3.75     10.67  76.9706    27.3   66.82
   120      26.09      4.08     11.85  76.9672    29.2   66.44
   180      26.09      4.16     12.17  76.9650    30.5   65.96
   210      26.09      4.22     12.25  76.9650    30.5   65.79
   240      26.09      4.29     12.25  76.9650    30.4   65.64
   360      26.09      4.92     14.92  76.9523    29.8   59.70
```

All the water is in by t = 60 min. The front then advances **10.43 → 12.25 km in three hours (0.17 m/s)**, with `lon_min` frozen at 76.9650 for 90 minutes, then jumps. That is bowl-filling: pause, fill to the sill, spill, advance.

`rishiganga`, coarsen 4, 7 hours, cascade central hydrograph on a dry bed (what production actually does — its initial-condition block raises and is caught):

```
 t_min  stored_Mm3  wet_km2  front_km  lon_min   max_d   v(h>1m)  Fr(h>1m)
    30      29.09      3.02      9.49  79.6201    41.2     24.21      5.97
    60      49.71      4.32     12.93  79.6007    82.7     86.24     10.01
   120      53.43      3.55     12.93  79.6007    97.9     93.03     10.42
   240      53.43      3.14     12.95  79.6007   113.8     79.49      7.54
   420      53.43      2.70     12.95  79.6007   113.4    104.99     11.84
```

**The front does not move for six and a quarter hours.** The wetted area *shrinks* from 4.32 to 2.70 km² while the maximum depth *grows* from 45.6 to 113.8 m. 53.4 Mm³ of water piles into a 113 m deep artificial lake 12.9 km downstream of the breach and stays there. That is the "artificial pond" exactly.

### 26.4 The escape route existed only through cell corners

After the outlet and the flowline conditioning were added, the escape head measured with **8-connectivity** looked fixed — and with the 4-connectivity the solver actually has, it had not moved at all:

| scenario | dx | 8-connected | 4-connected (what the FV scheme can use) |
|---|---|---|---|
| phutkal | 28 m | 6.1 m | **1548.8 m** |
| rishiganga | 28 m | 16.3 m | **1116.1 m** |

A finite-volume scheme exchanges flux across cell **faces**. Two cells touching only at a corner are not connected for it. The conditioned channel was stepping diagonally, and not one metre of it was usable. This was caught only because the escape metric was computed at both connectivities; it is the kind of defect that reads as "the fix didn't help much" rather than as a bug.

### 26.5 A note on the velocities in these tables

`|v|max` reaches 67 m/s (phutkal) and 330 m/s at t = 0 (coarsen 2), with Froude numbers to 17 in cells deeper than 1 m. **These are not physics.** They come from Part I's defect C2: 17,169 m³/s injected as a volumetric source into a 5×5 kernel of 112 m cells sitting at the bottom of a 58 m deep pool. The mound this creates collapses radially at `√(2gh)`. Nothing in Part II changes it, and no velocity reported by FloodSight near the breach should be believed until C2 is fixed.

---

## 27. Debris and intensity: what is missing

### 27.1 What the repository contains

Searched the whole tree for the vocabulary of sediment and debris physics:

```
grep -rniE "sediment|debris|rheolog|bingham|voellmy|yield.stress|entrain|scour|
            erosion rate|bulking|concentration|hyperconcentrat|two.phase|density"
            --include=*.py .
```

Five hits, **none of them physics**:

| hit | what it is |
|---|---|
| `data_fetcher.py:103` | the string `"High-altitude rock-ice avalanche debris barrier"` in scenario metadata |
| `data_fetcher.py:127` | the string `"Avalanche debris dam overtopping & catastrophic outburst"` |
| `data_fetcher.py:130` | a comment noting the 27 MCM rock-ice wedge |
| `event_graph.py:3` | a docstring saying this module does *not* model debris |
| `pysph_runner.py:110` | `rho=h_all` — depth encoded in a field named density, in a dead module |
| `exposure.py:218` | "uniform-density assumption" about population, not water |

### 27.2 Item by item

| Required for a debris flow | Present? |
|---|---|
| sediment/debris concentration | **No.** No state variable, no transport equation. |
| debris entrainment | **No.** |
| changing flow density | **No.** ρ does not appear anywhere in `swe_2d.py`; it cancels out of the clear-water equations and was never reintroduced. |
| changing viscosity / rheology | **No.** Resistance is a single Manning n. |
| momentum carried by debris | **No.** `hu`, `hv` are water momentum at constant density. |
| erosion / scour | **No.** The bed is fixed: `z` is read-only inside the integrator. |
| deposition | **No.** |
| blockage formation / removal | **No.** |
| downstream debris transport | **No.** |
| impact momentum on structures | **No.** Exposure uses depth alone; `_depth_damage_fraction` has no velocity or density term. |

**Ten out of ten absent.** FloodSight solves clear-water shallow water and nothing else.

### 27.3 Why a multiplier cannot substitute

The difference between clear water and a debris flow is not a coefficient, it is the governing system. A debris flow needs, at minimum:

1. a transported solid concentration `c(x, y, t)`;
2. mixture density `ρ_m = ρ_w(1 − c) + ρ_s c` appearing in **both** the pressure term and the momentum flux — 1.8–2.3× water, which changes the pressure gradient and the impact pressure, not just the magnitude of Q;
3. a non-Newtonian resistance closure — Voellmy (μ, ξ) or Bingham / Herschel–Bulkley (yield stress τ_y plus plastic viscosity) — replacing Manning, because a granular flow can stop on a slope where water would keep moving and can run out further on a flat where water would spread;
4. a bed entrainment/deposition law `E(x, y, t)`, since Chamoli-class flows *gain* most of their volume from the bed;
5. an Exner-type bed-evolution equation, so `dz_b/dt ≠ 0` and the terrain the flow routes over changes as it passes;
6. grain-size-dependent settling / phase separation for the coarse fraction;
7. impact pressure `ρ_m v²` for the consequence model.

Scaling Q by a factor changes (2) and nothing else, and gets (2) wrong because it raises the volume instead of the density. It would make the flood look bigger while remaining the wrong system of equations. **No multiplier was added.**

### 27.4 Which model each event class needs

| # | Event class | Governing model | Can FloodSight's solver do it? |
|---|---|---|---|
| 1 | ordinary clear-water dam break | 2D shallow water, fixed bed, Manning resistance | **Yes.** This is exactly what `swe_2d.py` solves, and it solves it well. |
| 2 | landslide-dam / GLOF outburst | SWE with a mobile bed and a bulked hydrograph; solids typically `c_v < 0.2`, so a Newtonian closure with elevated density and roughness is defensible; requires an erosion-limited breach model for the outflow | **Approximately.** Extent and arrival are usable; depth and momentum read low. Must be labelled. |
| 3 | avalanche / debris flow | Two-phase or mixture-theory depth-averaged equations with entrainment (Iverson-type two-phase, or Voellmy/Bingham single-phase mixture) over a mobile bed | **No.** Different system of equations. |
| 4 | river-blockage release | SWE plus an erosion-limited breach; if the blockage is granular and the release entrains it, this collapses into case 3 | **Partly**, and the case boundary is a real judgement each scenario must make and record. |

This taxonomy is now machine-readable: `src/m3_breach/FlowRegime` with `CLEAR_WATER`, `HYPERCONCENTRATED`, `DEBRIS_FLOW`, and `DEBRIS_FLOW_MISSING_PHYSICS` listing the eight items above next to the enum so the gap is specified rather than gestured at.

---

## 28. Real-disaster fidelity, revisited

### phutkal (2015 Phuktal/Tsarap landslide-dam outburst)

| question | answer |
|---|---|
| reaches the affected settlements? | Partly. `Ichar` — a documented loss — is reached. `Pibcha` (Pipcha) is not. `Padum` is outside the AOI. |
| correct valley pathway? | **Yes.** Bed falls monotonically westward along the wetted corridor; 256 of 302 newly-wet cells are west of the breach. |
| sufficient velocity and depth? | Depth is *too high* (2× the water, Part I C1) and near-breach velocity is an injection artefact (§26.5). |
| continues downstream, or ponds? | **Ponds.** Front frozen for 90 minutes at a time (§26.3). |
| arrival sequence sensible? | Unusable: the arrival raster marks the whole reservoir as reached at t = 0 (Part I M9). |
| enough momentum/volume for the observed destruction? | The volume is 2× too large and the momentum is not trustworthy. Neither can be compared to anything, because no observed extent exists for this scenario. |
| if it stops short, why? | Sealed domain (escape head 1548.8 m) plus a bowl-staircase channel (44.5 % adverse steps). Both now addressed; residual is resolution. |

### rishiganga (2021 Chamoli)

| question | answer |
|---|---|
| reaches the affected sites? | It reaches the NTPC Tapovan barrage — the documented downstream impact — at **25.4 min** against a documented **T+35 min**. That is the closest thing to a fidelity hit in the repository, and it is produced by a hydrograph whose peak, width and formation time are typed into config (Part I C8). |
| continues downstream? | **No.** Stationary for 6.25 hours, 113 m deep, 53.4 Mm³ trapped 12.9 km downstream. |
| same physical class of problem? | **No.** The 7 Feb 2021 event was a ~27 × 10⁶ m³ rock-and-ice avalanche that became a debris flow carrying >20 m boulders and scouring valley walls to 220 m above the floor; the debris-dammed lake formed *after* the flood. FloodSight models it as a clear-water reservoir with a rated spillway (`cd 1.8, length 20 m, max_q 2000 m³/s`) failing by Froehlich breach. A natural avalanche barrier has no discharge coefficient. The run now fails validity with `flow_regime: debris_flow` and the missing-physics list attached (§30, Fix 4). |

**The answer to "is FloodSight even solving the same physical class of problem" for Rishi Ganga is: no, and it now says so instead of producing a number.**

---

## 29. Fixes implemented, in dependency order

Each fix states the measurement that motivated it, what it does, what it refuses to do, and where the evidence lives. None of the banned remedies was used: no arbitrary water, no Q or velocity multiplier, no random channel carving, no lowering the DEM until water moved, no extended run time to reach a city, no hardcoded downstream polygons.

---

### Fix 1 — Give the domain an outlet
`src/m2_geometry/dem_utils.py::open_river_outlets` (new), called from `run_pipeline.py` after the geometry gate and the breach snap.

*Motivation:* escape head 1101–1572 m (§26.2); `volume_outflow_m3` identically zero in every run.

*What it does:* opens the no-data wall **only** where the mapped OSM river crosses from valid data out to the raster edge, sets those cells to the bed elevation the river already had at the crossing (minimum over the valid neighbourhood of the last on-river cell), and only for exits below the release elevation.

*What it refuses:* an exit whose bed is above the release elevation is not an outlet and is left walled — hydraulically inert, since a transmissive boundary above the water surface passes nothing either way, and it removes any chance of draining the impoundment out of the back of the domain.

*Rejected alternative, measured:* un-walling every edge-connected no-data cell by nearest-valid elevation. It invents terrain, and on phutkal it invented a passable corridor **around** the 4-cell barrier — the upstream pool and downstream valley merged into one connected component (1217 + 55 → 1274 cells) and `validate_geometry` correctly rejected the scenario. Recorded in the function's docstring so it is not retried.

*Result:* phutkal 18 cells opened at lon 76.910–76.914, bed 6504 → 3586 m, 21 km downstream. rishiganga 12 cells at lon 79.598–79.600, bed 6393 → 1699 m. Boundary cells below the release bed: **0 → 3** and **0 → 2**.

---

### Fix 2 — Make the mapped river a continuous pathway
`src/m2_geometry/dem_utils.py::condition_flowline` (new) + `_orthogonalise` (new).

*Motivation:* 44.5 % adverse steps and a 30 m sill on the breach reach (§26.1).

*What it does, in order:*
1. **Cross-stream snap.** Each flowline sample moves to the lowest cell within 60 m **along the local normal only**. This uses elevations the DEM already reports; nothing is invented. 60 m ≈ twice the source posting, the scale of OSM-vs-DEM positional disagreement — a horizontal tolerance, not a depth.
2. **Orthogonalise.** Insert the lower of the two detour cells at every diagonal step, so consecutive channel cells share a **face**. Without this the escape route existed only through corners and the solver could not use a metre of it (§26.4).
3. **Monotone filter.** Take the running minimum along the path and lower only the cells above it — removing adverse rises, never filling, never going below an elevation the DEM already reports upstream on the same reach.
4. **Protect.** The barrier and the upstream impoundment are never lowered and the running minimum restarts on the far side. Without this the filter would breach the dam by construction, because the barrier *is* an adverse rise on the flowline.
5. **Refuse.** A reach needing a cut deeper than 25 m is left untouched and counted. That is not a DSM staircase, it is the line crossing real ground.

*A correction made during this work:* the snap was first implemented as a **square** window. That let the path skip along-stream past a sill instead of resolving it — hiding the obstruction rather than finding the thalweg. Caught by `test_flowline_conditioning_removes_adverse_rises_on_the_channel`, which failed with `cells_lowered == 0`. Restricted to the cross-stream normal; every real-terrain number in this report is from the corrected version.

*Result:* see §30.

---

### Fix 3 — Differentiate channel roughness from floodplain roughness
`src/m4_solvers/roughness.py::apply_channel_roughness` (new), applied **after** the elevation banding and the GHS-POP override so it wins over both.

*Motivation:* §25 — channel n over 0.020–0.100 moves the front 4.26 → 1.70 km; floodplain n over 0.035–0.150 moves it not at all.

*What it does:* sets the conditioned channel's cells to `n = 0.035`. Source: Chow (1959) Table 5-6, natural mountain streams with gravel/cobble beds, n = 0.030–0.050. Overridable per scenario via `channel_manning_n`. It uses the **conditioned flowline's own cells**, not a fresh rasterisation of the OSM centreline, so the channel's geometry and its roughness refer to the same cells.

---

### Fix 4 — Refuse to present a debris flow as a clear-water flood
`src/m3_breach/__init__.py`: `FlowRegime`, `regime_status`, `parse_flow_regime`, `DEBRIS_FLOW_MISSING_PHYSICS`; `flow_regime` declared per scenario in `data_fetcher.py`; gate wired into the validity block in `run_pipeline.py`.

*Assignment, with the reason recorded next to each scenario:*

| scenario | regime | why |
|---|---|---|
| rishiganga | `debris_flow` | 27 × 10⁶ m³ rock-ice avalanche → debris flow; >20 m boulders; lake formed after the flood |
| south_lhonak | `debris_flow` | moraine-dam GLOF that transported boulders and destroyed a 60 m dam 42 km downstream |
| phutkal | `hyperconcentrated` | landslide-dam outburst through a gravel gorge; documented damage is water-dominated |
| derna, malpasset, ivanovo, annamayya | `clear_water` | engineered dams into wadi/valley/floodplain |

`debris_flow` sets `validity.valid = False` with the missing-physics list in `reasons`. `hyperconcentrated` proceeds with a logged caveat and the bias stated (depth and momentum read low) — solids at that concentration are still Newtonian enough for SWE. This follows the same fail-closed philosophy as the existing `require_implemented_mechanism`.

---

### Fix 5 — Real head for the injected momentum
`src/m3_breach/ensemble.py`: `Hydrograph.head_m`, filled by `route_breach`; consumed in `run_pipeline.py::_hydrograph_v_ms`.

*Motivation:* the non-cascade jet velocity was `Q / (width · dam.height_m)` with a **constant** head — it never fell as the reservoir drained, so the injected momentum stayed at its full-reservoir value through the entire recession. `route_breach` already computed the head on the invert every step to evaluate the weir and threw it away.

*Result:* measured on phutkal, the head now runs **44.9 m → 0.0 m** over the hydrograph instead of a flat 58.0 m.

---

### Fix 6 — Separate gross outflow from net boundary flux
`src/m4_solvers/swe_2d.py::_rhs` and `SimulationResult.volume_outflow_gross_m3`.

*Motivation:* `volume_outflow_m3` is a **net** flux. A transmissive boundary can let water back in, so the net alone cannot distinguish a sealed domain from one where inflow and outflow cancel. Conservation needs the net; "can water leave at all" needs the gross. Both are now tracked and both are reported.

---

### Fix 7 — `linemerge` on a single-reach river
`src/m2_geometry/dem_utils.py::_merged_parts`.

Found by the new tests: `linemerge` raises `ValueError: Cannot linemerge LINESTRING (...)` on a bare LineString in the installed shapely, which any scenario whose OSM waterways merge to one line would have produced. Pre-existing pattern, would have taken the run down.

---

## 30. Measured effect of the fixes

### Escape head — the decisive number

Head above the release bed at which the breach cell first connects to the raster edge, **4-connected**:

| scenario | dx | before | after |
|---|---|---|---|
| phutkal | 112 m | 1572.5 m | **36.5 m** |
| phutkal | 56 m | 1563.9 m | **27.0 m** |
| phutkal | 28 m | 1548.8 m | **7.5 m** |
| rishiganga | 113 m | 1101.5 m | **6.6 m** |
| rishiganga | 57 m | 1129.2 m | **23.7 m** |
| rishiganga | 28 m | 1116.1 m | **17.2 m** |

From a kilometre and a half to metres, and it improves with resolution on phutkal (36.5 → 27.0 → 7.5 m) — the residual is a resolution limit, not a tuned constant.

### Channel profile on the breach reach, along the conditioned thalweg

| scenario | dx | adverse steps | adverse rise | max sill | points needing > 2 m |
|---|---|---|---|---|---|
| phutkal | 28 m | 77/156 → **17/153** | 183 → **46 m** | 22.0 → 22.0 m | 127/157 → **44/154** |
| phutkal | 56 m | 41/80 → 41/80 | 268 → 268 m | 27.0 m | 69/81 |
| phutkal | 112 m | 20/38 → 20/38 | 321 → 321 m | 36.5 m | 33/39 |
| rishiganga | 28 m | 34/103 → **15/103** | 123 → **68 m** | 22.5 m | 44/104 → 43/104 |
| rishiganga | 57 m | 19/54 → **8/54** | 91 → **43 m** | 22.3 → 18.5 m | 16/55 → 10/55 |
| rishiganga | 113 m | 9/25 → 9/25 | 91 → 91 m | 30.6 m | 9/26 |

**At 112 m the breach reach is unchanged, because the cut it needs exceeds the 25 m cap and the code refuses it** — 27/80 reaches refused on phutkal, 57/99 on rishiganga. That refusal is the honest outcome: at that cell size the gorge cannot be conditioned without digging a fabricated trench. At 28 m the conditioning works and the required cuts fall inside the cap.

### Terrain actually modified

| scenario | dx | cells lowered | max drop | mean snap move | volume removed |
|---|---|---|---|---|---|
| phutkal | 28 m | 1797 | 23.3 m | 31 m | 4.26 × 10⁶ m³ |
| rishiganga | 28 m | 865 | 24.7 m | 30 m | 4.33 × 10⁶ m³ |

Against a 1134 × 1189 tile at 28 m, that is 0.13 % of cells. Every figure is written into the run manifest under `flowline_conditioning`.

### Water leaving the domain

`rishiganga`, coarsen 4, 7 hours, identical forcing:

```
baseline (sealed)                    outflow_gross = 0.000e+00 m3   (exactly zero)
outlet + flowline + channel roughness outflow_gross = 4.774e-03 m3   (non-zero)
```

The topology changed: the boundary is reachable. The magnitude is small because at 113 m the route out is a single-cell slot, which is the same resolution limit as above.

### What did not change much, and why

At coarsen 4 the full 2D comparison moves the front by 2 % (phutkal 14.92 → 15.16 km over 6 h) and at coarsen 2 by 4 % (15.43 → 16.03 km over 2 h, and 13.56 → 14.46 km at t = 70 min, +7 %). **At 112 m cells these fixes cannot do much, and the code says so rather than compensating.** The gorge is 50 m wide and the cell is 112 m; no amount of conditioning recovers a channel that is not in the data. The honest path to a large improvement is resolution (28 m, where the escape head drops to 7.5 m) or sub-grid channel conveyance (§32).

---

## 31. Before / after physical data-flow

**Before**

```
Copernicus GLO-30
   │
   ├─ condition_dem ─── every no-data cell -> max(z)+100 m
   │                    94-98% of the boundary ring walled
   │                    escape head 1.1-1.6 km  ── DOMAIN SEALED
   │
OSM waterways ──► build_rivers ──┬──► snap_to_thalweg   (a seed point)
                                 └──► SPH thalweg       (returns NOT_AVAILABLE)
                                        │
                                        ╳  never reaches the solver
                                        │
roughness = f(elevation bands) then f(GHS-POP)          ── no channel class
   │
   ▼
2D SWE  ── flow direction from bed slope alone
        ── channel is whatever the 30 m DSM happens to show:
           44.5% adverse steps, sills to 65 m
        ── water fills bowl, pauses, spills, fills next bowl
        ── outflow ≡ 0.000e+00, stored rises monotonically
   │
   ▼
"flood" = a 113 m deep artificial pond 12.9 km downstream
```

**After**

```
Copernicus GLO-30
   │
   ├─ condition_dem ─── interior no-data holes still walled (fail-safe)
   │
OSM waterways ──► build_rivers ──┬──► snap_to_thalweg (unchanged)
                                 │
                                 ├──► open_river_outlets
                                 │      opens the wall only where the river
                                 │      leaves the data, at the bed it had,
                                 │      only below the release elevation
                                 │      escape head 1.1-1.6 km -> 6.6-36.5 m
                                 │
                                 ├──► condition_flowline
                                 │      cross-stream snap (60 m, normal only)
                                 │      -> orthogonalise (face-connected)
                                 │      -> monotone filter (lower only)
                                 │      -> protect barrier + pool
                                 │      -> refuse cuts > 25 m, count them
                                 │      adverse rise 183 -> 46 m @ 28 m
                                 │      returns channel_mask ──┐
                                 │                             │
                                 └──► apply_channel_roughness ◄─┘
                                        n = 0.035 on those same cells
   │
   ▼
2D SWE  (unchanged — it was always correct)
        ── responds monotonically to slope, width, incision, channel n (§25)
        ── supercritical flow reached where geometry produces it
        ── gross AND net boundary flux tracked separately
   │
   ▼
flow_regime gate ── clear_water: proceed
                 ── hyperconcentrated: proceed, bias stated
                 ── debris_flow: valid = False + missing-physics list
```

---

## 32. What is still missing

Ordered by how much it distorts the answer.

1. **Part I's C1/C2/C3 are untouched.** The reservoir's water is still supplied twice, the breach still injects into the pool at its deepest cell, and the barrier is still absent from the solver's terrain. Every velocity near the breach in this report is an artefact of C2. **Part II makes the downstream pathway real; it does not make the release real.** Fixing C2 is now the single highest-value change left.
2. **Sub-grid channel conveyance.** The proper answer to a 50 m gorge in a 30 m cell is not to condition the terrain but to give each channel cell a sub-grid conveyance (channel width, depth and roughness carried per cell, with the cell's discharge computed from the channel cross-section rather than the cell-average depth). This is the standard 2D-with-sub-grid-channel approach (LISFLOOD-FP-style). It would remove the need for the monotone filter, and it is a solver change rather than a preprocessing change.
3. **Channel width and depth are not used at all.** OSM carries `width` on some waterways; nothing reads it. Hydraulic-geometry relations (`w ∝ Q^0.5`, `d ∝ Q^0.4`) would give a defensible width where OSM is silent.
4. **No Froude diagnostic in production.** It is computed nowhere. A per-cell Froude field would make transcritical behaviour visible and would flag the injection artefact automatically.
5. **Debris-flow physics.** Declared and gated, not implemented. §27.3 lists the seven additions required. This is a research task, not an engineering one, and must not be faked with a multiplier.
6. **Backwater at the outlet.** The opened outlet is a flat sill at the exit bed. A rating curve or a normal-depth boundary would be more correct; the flat sill is a stated approximation.
7. **The 25 m refusal cap is a policy constant.** It is justified as "deeper than this is not a DSM staircase", and it demonstrably binds at 112 m and not at 28 m. It has not been calibrated against surveyed cross-sections, because none exist for these reaches.

---

## 33. Verification

- Full suite: **167 passed** (153 pre-existing, all still passing, plus 14 new) in 426 s.
- New tests, `tests/test_river_pathway.py`, pin the properties the solver depends on: every channel step shares a cell **face**; the detour taken at a diagonal is the lower one; conditioning removes adverse rises, **never raises terrain**, **never touches the protected pool**, and **refuses and leaves untouched** a cut deeper than the cap; the outlet turns a sealed domain (escape head > 50 m) into one water leaves with ≤ 1 m of head, **only lowers** the wall, and **refuses** an exit above the release elevation; the regime gate classifies all four cases and **raises rather than defaulting** on an unknown regime.
- Two of these tests found real defects during development: the square snap window (which hid sills instead of resolving them) and the `linemerge` crash on a single-reach river.

### Experiments in this part

| ID | What it measured |
|---|---|
| E6 / E6b | The mapped river's profile on the real DEM — adverse steps, adverse rise, downstream sills, ponding depth needed |
| E7 | Solver response to channel presence, slope, width, incision, channel n and floodplain n on an idealised channel |
| E9 | Full 2D, four arms (baseline / +outlet / +flowline / +channel roughness), phutkal coarsen 4 |
| E10 | Front position and stored volume vs time, phutkal coarsen 2 |
| E11 | 6-hour phutkal run, sealed vs opened |
| E12 | 7-hour rishiganga run: the 113 m deep stationary pond, arrival at Tapovan, first non-zero gross outflow |
| escape-head | Bottleneck (minimax) head from the release cell to the raster edge, at 4- and 8-connectivity, before and after, at three resolutions |
| snap-radius | Cross-stream minimum search radius vs adverse-rise and sill statistics |

---

## 34. Answer to the closing question

> "Does FloodSight actually know where the water should go, how fast it should go, how its energy changes as terrain/channel geometry changes, and what happens to that flow when it carries sediment/debris?"

- **Where the water should go:** now, largely yes. The mapped river reaches the solver as a face-connected, monotone channel with its own roughness, and the domain has an outlet. Before Part II: no — the river was used for a seed point and nothing else, and the domain was sealed.
- **How fast:** the solver's response to slope, width, incision and channel roughness is correct and monotone, and supercritical flow is reachable (§25). But the velocity field **near the breach** is dominated by the volumetric injection artefact and must not be believed until Part I C2 is fixed.
- **How its energy changes with geometry:** correctly, through the well-balanced bed-slope source and point-implicit friction — now that the geometry is actually there.
- **What happens when it carries sediment or debris:** **nothing.** That physics does not exist in the repository, in any form. The model now refuses to call such a run valid and names the eight things it would need, instead of returning a clear-water number for a granular event.

---
---

# Part III — Plan for C1 / C2 / C3 (design only, nothing implemented)

**Date:** 2026-09-12
**Status:** **NOT IMPLEMENTED.** This part contains no code changes. It is the design for the three Part I critical defects that Part II deliberately did not touch, written down so the next session starts from a decision rather than a rediscovery.

Part II made the *downstream pathway* real. It did not make the *release* real. Everything below is about the release.

---

## 35. Why these three are one job

| | defect | Part I ref |
|---|---|---|
| **C1** | The impoundment's water is placed in the domain twice — once as `initial_depth`, once as the injected hydrograph. Measured **2.00×** | §4 C1 |
| **C2** | The breach hydrograph is injected as a 5×5 Gaussian volumetric source at the pool's *deepest cell* — i.e. into the reservoir, not out of it | §4 C2 |
| **C3** | The barrier is raised only inside a private copy of the DEM; the array the solver integrates has never had a structure in it | §4 C3 |

They are one defect wearing three faces, and the causal chain runs backwards:

```
no structure in the solved terrain (C3)
   └─► nothing for a breach to open
          └─► discharge has to be faked as a volumetric source (C2)
                 └─► the reservoir must be supplied separately from its own drainage (C1)
```

Fixing any one alone does not work:

- Fix C1 alone (drop `initial_depth`) → the flood is the right volume but still appears out of nothing at the pool floor, and there is still no impoundment to fail.
- Fix C2 alone (move the injection downstream of the barrier) → still an injection, still double-counted, and the "barrier" it is downstream of does not exist in the terrain.
- Fix C3 alone (emplace the barrier) → the injection now fills a real reservoir that cannot drain, and the double count gets worse.

**They must land as one change, in the order P2 → P3 below, behind the P1 gate.**

---

## 36. P1 — Prerequisite: make the mass gate able to fail

Part I **C5**, already stated in §20 Phase 1. Restated here because P2/P3 must not be attempted without it.

The current gate is `|((initial + injected) − stored − outflow + clipped)| / total_in < 0.01`. Because `initial` counts as input and `clipped` is added back as accounted mass, the identity holds for any run where water stays put. Measured: the production run (2× water), the `Q=0` run and the dry-bed run all score **0.00000 %**.

**Add three independent gates:**

| gate | test | why |
|---|---|---|
| G1 volume provenance | `abs(initial + injected − impounded_volume) / impounded_volume < tol` | this is the gate that fails on C1 today |
| G2 manufactured mass | `clipped / total_in < tol`, as a **failure**, not an accounted term | clipping currently absorbs manufactured water silently |
| G3 reachable outlet | `escape_head_4connected(release_cell) < tol` on the conditioned DEM | Part II added the outlet; nothing yet asserts it exists |

`escape_head_4connected` is the bottleneck (minimax) search used throughout Part II §26.2/§30. It is ~25 lines and already written twice in the scratchpad experiments; it belongs in `src/m2_geometry/` as a function, not a test helper.

**Acceptance:** the certified run `data/scenarios/7d96e858d7c74eefacf2b79edcff9610` must **fail** G1 after this lands, reporting `initial + injected = 2.00 × impounded`. If it still passes, the gate is still a tautology.

**Effort:** small. No physics changes. Do this first and alone, so the P2/P3 work has an instrument that can tell it whether it worked.

---

## 37. P2 (C3) — Put the barrier in the terrain the solver integrates

### What changes

`fill.py:130` and `fill.py:260` already perform exactly the right operation:

```python
dem = np.where(barrier_mask & (dem < barrier_crest_m), barrier_crest_m, dem)
```

on a **private copy**. The change is to perform it once, on `dem_elev`, before `build_stage_storage` and before the solver, so the fill, the initial condition and the hydraulics all see one terrain. The private copies then go away.

Placement: immediately after the geometry gate and after `dem_crest_along_axis` is sampled — the same slot Part II's `open_river_outlets` / `condition_flowline` occupy, and **before** them, so the flowline conditioner's `protect_mask` is protecting a barrier that actually exists.

### The blocker: where does the crest elevation come from?

`barrier_crest_m = wse_m + 5.0` appears at six call sites (`run_pipeline.py` lines ~490, 692, 862, 880, 1143, 1158). It is an arbitrary 5 m freeboard whose only job is to make the seeded fill close. It **must not** become the emplaced structure's crest — that would be a fabricated dam.

Three candidate sources, in order of preference:

1. **A new required `crest_elev_m` on the geometry manifest**, carrying `source` and `classification` like every other manifest field. This is the correct answer and it is scenario-authoring work: 6 manifests (7 scenarios minus `south_lhonak`, which has no manifest at all — Part I C6).
2. `dem_crest_along_axis` — already sampled at `run_pipeline.py:364-380`, but it is `max(DEM)` over a ±250 m window around the breach point, i.e. the valley walls, not the structure. Usable as a *bound*, not as the crest.
3. The cascade config's `reservoir.z_crest_m` — already the authority for `wse_m` on that path, but it is in a different datum regime for `south_lhonak` (Part I M14) and must be reconciled first (Part I C10).

**Decision to make before coding:** adopt (1) and fail the geometry gate for any scenario without a sourced crest, consistent with how the gate already treats every other missing role. Do not fall back to (2) or to `wse+5`.

### Also needed on the manifest

A **`dam_axis`** role (LineString). No scenario carries one today (Part I FS-14, restated) — the breach line is being used as the axis. P3 needs an axis to measure breach width along; without it "breach width" has no direction.

### Acceptance test

Re-run Part II's **E5** — the initial pool with `Q = 0` for 1800 s:

```
                           wet t=0    wet t=1800    spread beyond pool
as found (no barrier)          35          195             161 cells
barrier raised (private copy)  35          185             150 cells
TARGET after P2                35           ~35              ~0 cells
```

The impoundment must **hold**. If the pool still escapes with `Q = 0`, the barrier is not continuous at run resolution (`phutkal`'s `barrier_mask` is **4 cells** at coarsen 4, 51 at coarsen 1) and the geometry gate should be saying so — add an abutment-keying / continuity check to `validate_geometry`, which currently checks that the barrier *intersects* the breach zone and the river but never that it *spans* the valley.

---

## 38. P3 (C2 + C1) — Cut the opening; make Q an output

This is the substantive change. After P2 there is a structure holding water back; P3 makes a hole in it and lets the 2D solver drain the pool through it.

### 38.1 Where the opening goes

Stop using `snap_to_thalweg` to place the release. It searches for the lowest cell within 300 m and therefore **always** lands on the pool floor — that is the mechanism of C2, not an accident (Part I M22).

Use `validate_geometry`'s `breach_mask`, which is already computed, already validated against the barrier and the river, and currently **thrown away** (Part I M12). `downstream_source_weights` is also already computed and discarded; it is the per-cell weighting an opening needs.

### 38.2 The opening evolves

The breach kernel already exists and is shared and verified (`breach_kernel.py`, Part I §9). It currently drives a 0-D weir. Instead, per timestep:

```
invert(t) = breach_invert_at(t − t_trigger, t_f, z_crest, z_bed)
width(t)  = breach_width_at (t − t_trigger, t_f, B_final)
opening   = cells of breach_mask within width(t)/2 of the breach centre,
            measured along dam_axis
z_solver[opening] = max(z_natural[opening], invert(t))
```

So the terrain the solver integrates changes with time: the dam erodes down and sideways, and the pool drains through it because the free surface is higher than the invert. No source term.

### 38.3 The solver change this requires

`z` is currently read-only inside the integration loop (`swe_2d.py`). It becomes mutable. Two things need care:

- **Lowering the bed under standing water.** Hold the **free surface** η constant, not the depth: `h += (z_old − z_new)` on wet cells. Holding `h` instead would create water. This is a storage re-attribution and must be accounted as such in the ledger, with its own counter (`volume_bed_lowering_m3`), not folded into `clipped`.
- **Well-balancedness.** `_rhs` re-derives the interface bed from `z` on every call, so a changing `z` is fine — but the lake-at-rest benchmark must be re-run after the change, because the exact cancellation is the property that makes this solver worth keeping (Part I §16.1).

### 38.4 Q becomes a measurement

Sum the face fluxes across the opening's downstream faces each step:

```
Q_breach_measured(t) = Σ F_h over the opening's downstream faces × cell width
```

Write it to `hydrograph.json` **alongside** the 0-D routed arm. The 0-D reservoir model (`cascade.simulate_reservoir_cascade` / `ensemble.route_breach`) is not deleted — it is **demoted from driver to check**. Comparing routed Q(t) against measured Q(t) is the first genuine internal validation this codebase would have.

### 38.5 What gets deleted

- The Gaussian injection kernel for any scenario with a barrier (`swe_2d.py:709-723`, `798-817`). Keep it only for the classical benchmarks that legitimately have no structure.
- `initial_depth` stays — it is now the *only* source of the reservoir's water, which is what makes C1 close by construction.
- `barrier_crest_m = wse_m + 5.0` at all six call sites.

### 38.6 Acceptance tests

| # | test | as found | target |
|---|---|---|---|
| A1 | total water in the domain ÷ impounded volume | **2.00×** | 1.00 ± tol |
| A2 | max free surface − reservoir WSE (superelevation) | **+115 m**, 101 cells above WSE | ≤ a few metres |
| A3 | max \|v\| near the release, cells with h > 1 m | **67 m/s** (coarsen 4), **330 m/s** (coarsen 2), Fr to 17 | ≤ √(2gH) ≈ 34 m/s for H = 58 m |
| A4 | measured Q(t) peak vs routed Q(t) peak | not comparable — one does not exist | within a stated band, and the band is a finding either way |
| A5 | `Q = 0` before `t_trigger` | injection starts at t = 0 regardless | domain is quiescent; pool held; stored constant |
| A6 | P1 gate G1 | passes at 2.00× | passes at 1.00× |
| A7 | Ritter + lake-at-rest benchmarks | pass | still pass after `z` becomes mutable |

A3 is the one to watch: the √(2gH) bound is the free-fall speed of the impounded column and no dam-break jet should exceed it by much. The current 330 m/s is nine times it.

---

## 39. P4 — Stop counting the reservoir as flood

Part I **M9**, independent of P1–P3 and cheap, but it must land in the same pass or the exposure numbers will still be wrong after the physics is right.

`swe_2d.py:706-707` seeds `max_h` and `arrival_time` from `initial_depth`, so the reservoir is inside `max_depth.tif` — which `compute_village_exposure`, the arrival raster, the envelope and any future CSI all read. Measured on the certified run: **67 of 402 max-depth cells (17 %) are the reservoir**, two villages report `max_depth_m = 58.0` (exactly the reservoir depth), and the arrival raster marks the whole pool as reached at `t = 0`.

Fix: carry `initial_depth` through to the consequence stage and sample exposure from `max(max_depth − initial_depth, 0)`; exclude cells wet at `t = 0` from the arrival raster. Keep the raw `max_depth.tif` for the map, which should show the reservoir.

---

## 40. Ordering, dependencies and effort

```
P1  mass gates that can fail            ── small, no physics, DO FIRST
     │
     ├── (data) geometry manifests gain crest_elev_m + dam_axis, sourced
     │          6 manifests; south_lhonak has none at all
     │
P2  emplace the barrier (C3)            ── small code, blocked on the data above
     │   acceptance: pool holds with Q = 0
     │
P3  cut the opening, Q as output (C2+C1) ── the substantive change
     │   needs: mutable z, η-preserving bed lowering, face-flux integration,
     │          injection deleted, 0-D model demoted to a check
     │   acceptance: A1-A7 above
     │
P4  exposure excludes the initial pool   ── small, land in the same pass
```

**Do not reorder.** P3 before P2 re-creates C1 in a worse form. P2 before P1 leaves no instrument to tell whether it worked.

**Everything in Part II stays.** The outlet, the flowline conditioning, the channel roughness and the regime gate are upstream of this work and are what make a correctly-released flood able to go anywhere.

---

## 41. What this plan must not do

Carried forward from the original brief and from what has already been tried and rejected:

- No arbitrary water, no Q multiplier, no velocity multiplier, no debris multiplier.
- No carving channels where no river is mapped; no lowering the DEM until water moves.
- No extending the simulation until water happens to reach a settlement.
- No hardcoded downstream flood polygons.
- **No `barrier_crest_m = wse_m + 5.0` surviving into the emplaced structure.** A dam whose crest is defined as "5 m above whatever water level we assumed" is a fabricated dam.
- **No keeping the injection alongside the opening "for comparison."** That reinstates C1.
- **No tuning the breach growth law so measured Q matches routed Q.** The disagreement between them is the finding.
- If the crest elevation or the dam axis cannot be sourced for a scenario, that scenario **fails the geometry gate**, exactly as five of them already do. It does not get a default.

---

## 42. Open register after Parts I–III

| id | defect | severity | status |
|---|---|---|---|
| C1 | reservoir water counted twice (2.00×) | Critical | **open — planned, §38** |
| C2 | breach injected into the pool at its deepest cell | Critical | **open — planned, §38** |
| C3 | no barrier in the solver's terrain | Critical | **open — planned, §37** |
| C4 | domain has no outlet | Critical | **fixed** (Part II Fix 1); escape head 1101–1572 m → 6.6–36.5 m |
| C5 | mass gate cannot fail | Critical | **open — planned, §36 (P1)** |
| C6 | 6 of 7 scenarios cannot run | Critical | open; `validate_geometry:185` label bug (M13) is the cheapest first experiment |
| C7 | annamayya never triggers (peaks 0.48 m short of crest) | Critical | open — scenario forcing, needs sourced agency figures |
| C8 | cascade breach params are typed config labelled Froehlich | Critical | open |
| C9 | fabricated Malpasset `simulated_*` served live to the UI | Critical | open — Phase 0, delete |
| C10 | rishiganga DEM impoundment 64,600× configured; two water levels | Critical | open |
| — | river never reached the solver | Critical | **fixed** (Part II Fix 2/3) |
| — | conditioned channel diagonal-only, unusable by the FV scheme | Critical | **fixed** (Part II Fix 2) |
| — | no debris physics; debris events presented as clear water | Critical | **gated** (Part II Fix 4) — declared and refused, not implemented |
| M1–M25 | see §4 | Major | open, except M12 (partially — `breach_mask` still unused, and P3 is what would use it) |

---

## 43. One-line state of the model after Part II, before Part III

> The water now knows where to go and can leave the domain, and a debris event is refused rather than faked. **It is still twice as much water, appearing in the middle of a reservoir that nothing is holding back.**

---
---

# Part IV — Status of the 2026-09-11 deep-review backlog (FS-01 … FS-56)

**Date:** 2026-09-12. **Method:** every row of `FLOODSIGHT_DEEP_REVIEW_2026-09-11.md` §C re-checked against the working tree by grep, import trace or direct measurement. The doc's own claims and the "audited all 55, fixed 2" note in `findings_results.md` were **not** taken as evidence. `FS-14` has no table row (it appears only in prose) and is included below.

**Tally: 14 closed and verified · 6 closed by inspection, not re-run · 10 partial · 26 open.**

Three of the open ones are the same defects Part I found independently: **FS-06 ≡ C2**, **FS-46 ≡ C9**, and **FS-09's "fix" is what created C1**.

---

## 44.1 Closed — verified in the working tree

| ID | was | verified now |
|---|---|---|
| FS-01 | every run marked `failed`; no `validity` key | `run_pipeline.py` returns a real `validity` block; the worker gate is intact and still fails closed |
| FS-02 | `_JOBS` never left `"queued"`; `_async_job_runner` had no callers | `grep -rn _async_job_runner` → **no hits**; `_reconcile_job` rebuilds `_JOBS` from the manifest on every read |
| FS-04 | reservoir initialised above the impounding terrain | crest gate at `run_pipeline.py:1523` exists and **fires** — both archived rishiganga runs fail on it with `WSE 2393.0 > DEM crest 2384.13` |
| FS-10 | "lake formation" produced by raising a 200 m-radius disc | `barrier_mask` from `validate_geometry` is the path taken; `barrier_xy`/`barrier_radius_m` is a fallback the pipeline never passes |
| FS-12 | hand-authored 8-vertex reservoir polygon on the map | `map.js:335` — "reservoir_pool is intentionally never populated" |
| FS-13 | hand-drawn Cheyyeru channel climbing valley walls | no river/channel polyline left in `map.js` |
| FS-21 | `hydraulic_ready` permanently `False` | `scenarios.py:155` sets it from `validate_geometry`'s real verdict; `tests/test_geometry_manifests.py` pins that it is not hardcoded |
| FS-24 | `egress_score` constant, +0.12 to every score | `ranker.py:144` computes it from `n_exits` |
| FS-25 | `score_hi`/`score_lo` read as ensemble spread | renamed `score_sensitivity_hi`/`_lo`, documented at `ranker.py:103` as a fixed ±15 % sensitivity band |
| FS-31 | Annamayya duration floor applied to every cascade | per-scenario `min_coverage_duration_s`; only annamayya declares one |
| FS-37 | ensemble hydrograph never rendered | `charts.js:93` plots all three arms |
| FS-44 | `breach_cd` read from config, set by nobody | symbol gone entirely |
| FS-52 | engineering override never reached formation time | `ensemble.py:88` applies the 4× non-erodible multiplier; measured: `dam_type=concrete` moves t_f 0.349 → 1.396 h and the routed peak 16,324 → 10,415 m³/s |
| FS-55 | two disagreeing breach-growth kernels | `breach_kernel.py` is the single shared trapezoidal DAMBRK kernel; both live paths call it; coefficients verified (Part I §9) |

## 44.2 Closed by code inspection — not re-verified on a run

Landed in the P1-3 / P1-4 work. The code reads correct; no end-to-end run was made to confirm the output changed, because 6 of 7 scenarios cannot run (FS-03/C6).

`FS-23` isolation before water arrives · `FS-27` silent dry on mask failure · `FS-28` CAP emitting UTM as lat/lon · `FS-29` stage-storage fallback invisible · `FS-30` one-arm envelope indistinguishable from three · `FS-45` API's two sources of truth

## 44.3 Partial — changed, but not closed

| ID | what changed | what is left |
|---|---|---|
| FS-07 | `carve_breach_geometry` is now unreachable (only annamayya declares a centreline, and it fails the gate first) | Part II replaced it with `condition_flowline`, which **is** still terrain excavation before the solve — but bounded, measured, capped, refused over 25 m, and reported in the manifest (0.13 % of cells at 28 m). Reclassified, not eliminated |
| FS-09 | domain no longer starts dry on the **non-cascade** path | it still starts dry for **cascade** scenarios (the IC block raises `levels_m must lie within connected pool stage range` and is caught — `initial_condition_source: null` in both archived rishiganga manifests), and the fix is what created **C1**, the 2.00× double count |
| FS-11 | pre-breach frames labelled `ASSUMED_FRACTION` | the invented values are unchanged: `lake_fractions=(0.25,0.50,0.75,0.95)` at `t = −120/−60/−30/−10 min` |
| FS-17 | `_validate_source_identity` refuses everything except Derna | the fabricated `annamayya_observed_extent.geojson` is still on disk and still listed in `observed.SOURCES` with `classification: "OBSERVED"` |
| FS-33/34/35/36 | `Promise.all` frame fetch + idle PNG prefetch in `map.js:1850-1918` | payload sizes and per-tick re-tiling not re-measured; cannot be, until a scenario runs |
| FS-53 | `test_swe_gpu.py` has a real `skipif` | `test_lake_cascade_gee.py` still has none — it passes without the backend |
| FS-56 | `FailureMechanism` + `require_implemented_mechanism` exist, and Part II added `FlowRegime` | **no cascade scenario sets `failure_mechanism`** — measured: all four are `None`, so every one runs `OVERTOPPING_EROSION` by default, and both implemented mechanisms are bit-identical anyway (Part I M3) |

## 44.4 Open

### Same as a Part I critical — see Part III for the plan

| ID | | |
|---|---|---|
| FS-06 | breach/inflow point ~545 m **inside** the reservoir | **= C2.** Measured: the injection cell carries 58.0 m of initial-condition water, i.e. it is the pool floor. Planned §38 |
| FS-14 | no scenario carries a `dam_axis` distinct from the breach line | **blocks P2/P3** — "breach width" has no direction without it. Planned §37 |
| FS-46 | Malpasset ground-truth file ships its own simulated answers | **= C9.** `grep -rn malpasset_benchmark` → only the endpoint that serves it and the frontend that renders it. Nothing writes it; malpasset has never run |

### Data and provenance

| ID | status |
|---|---|
| FS-03 / FS-48 | **125 of 128** run directories still have no manifest |
| FS-05 | annamayya DEM still holds a flat 192.50 m water plane against a configured 180.0 m bed |
| FS-17 / FS-47 / FS-51 / FS-54 | the fabricated artefacts are all still on disk: annamayya extent, ivanovo extent (incl. a 5-vertex rectangle), five ~1 KB "Sentinel-1 SAR" polygons, and `data/validation/README.md` still says "Nothing here is synthetic" |
| FS-49 | `observation_manifest.py` still reads `data/observations/`, which **does not exist**; the unvalidated glob path in `observed.py` is what actually loads data |
| FS-50 | building footprints still carry **zero properties** — measured: annamayya 191, derna 19 644, ivanovo 4 228 features, `properties` keys `[]` |
| FS-43 | `south_lhonak` still describes two structures under one key, and has **no geometry manifest at all** (`south_lhonak.json` missing; `_chungthang` and `_moraine` exist) |

### Frontend claims the backend does not support

| ID | status |
|---|---|
| FS-15 | the Delft3D-FLOW benchmark is still displayed |
| FS-18 | the UI still brands "Sentinel-1 SAR (GEE)" under "Satellite Ground Truth" |
| FS-19 | `spine.js:198` still paints "Full washout of 336 m earthen bund at 06:15 AM IST (T+30 min), peak release ~12,200 m³/s (EVD-17/18)" — for a scenario whose breach discharge is identically zero (Part I C7) |
| FS-26 | 19 invented shelters still in `map.js`, rendering like real OSM facilities |
| FS-32 | worse than reported: `wse_m` is not merely inconsistent on first load, it is **entirely dead** — overwritten at `run_pipeline.py:444/447` before first use (Part I M1) |
| FS-22 | scenario truth still duplicated — 8 hardcoded `wse:` values in `map.js` alone (2450 for rishiganga against the engine's 2393) |

### Architecture and physics

| ID | status |
|---|---|
| FS-08 | three `scenario_key == "annamayya"` branches remain in `run_pipeline.py` (673, 720, 1774) |
| FS-16 | the SPH scenario comparison still returns `{"available": false, "status": "NOT_AVAILABLE"}`; the code below it is still unreachable |
| FS-20 | `event_graph.py` still imported by nothing but its own test. Part I recommends **deleting** it rather than wiring it — the kernel consolidation (FS-55) made the migration it existed for obsolete |
| FS-38 | the fixed-grid-fraction breach fallback is still there, though unreachable (every scenario has `breach_lon/lat`) |
| FS-39 | **measured and confirmed open**: `von_thun.py:77` reuses Froehlich's Q_p regression verbatim — `Froehlich Qp == VonThun Qp` is `True` to every digit — and `macdonald.py:87` reuses Froehlich's t_f. Breach width across the three spans 16.4 / 84.1 / 199.9 m |
| FS-40 | at least three independent arrival-time computations remain (`swe_2d.py:727`, `run_pipeline.py`'s raster pass, `compare_arrivals.py`) |
| FS-41 | `src/api/main.py` still returns `"phutkal", "Phutkal River Landslide Dam"` for an unidentifiable run, at three call sites |
| FS-42 | the three diagnostic scripts are still byte-duplicated at the repository parent |

---

## 44.5 Where the remaining work actually lives

The FS backlog and Parts I–III converge on six clusters. Everything still open belongs to one of them.

| cluster | FS ids | Part I/III ref |
|---|---|---|
| **1. The release is fake** — no barrier, injection into the pool, water counted twice | FS-06, FS-09, FS-14 | C1/C2/C3 → **Part III P1–P4** |
| **2. Six of seven scenarios cannot run** | FS-03, FS-05, FS-43, FS-48 | C6, C7, C10 |
| **3. Fabricated artefacts still shipped** | FS-15, FS-17, FS-46, FS-47, FS-49, FS-51, FS-54 | C9, M10, M11 → Phase 0, delete |
| **4. UI asserts what the backend cannot do** | FS-18, FS-19, FS-22, FS-26, FS-32 | M1, M15, M16, M17 |
| **5. Breach parameterisation is input, not output** | FS-39, FS-56 | C8, M3, M5–M8 |
| **6. Dead weight** | FS-16, FS-20, FS-38, FS-40, FS-41, FS-42 | M18, M19 |

Cluster 3 is the cheapest and the most indefensible if shown to a reviewer — it is file deletions and string edits, no physics. Cluster 1 is the one that makes the model mean anything.

---
---

# Part V — Root-cause investigation of the remaining physical/numerical failures

**Date:** 2026-09-12 (second pass, after P1–P4 landed and the suite reached
185 passed / 2 xfailed).
**Status:** **DIAGNOSIS AND PLAN ONLY. ZERO CODE CHANGES IN THIS PASS.**
Every number below was reproduced in this session against the current working
tree. Where a Part I–IV claim is superseded, the superseding measurement is
given; where a claim is confirmed, it is marked confirmed rather than repeated.

Confidence is labelled throughout as **CONFIRMED** (mechanism traced to the line
and reproduced), **STRONG HYPOTHESIS** (consistent with every measurement taken,
not yet isolated), or **UNKNOWN**.

---

## 45. A — Confirmed root causes

### 45.1 RC-1 — The scheme is well-balanced only where the bed's discrete Laplacian vanishes — **CONFIRMED**

`swe_2d._rhs` builds the cell bed it reconstructs against as

```python
zi    = 0.5 * (z[k] + z[k+1])          # interface bed, single-valued
z_m   = zi[i-1] ;  z_p = zi[i]         # cell i's two edge beds
z_bar = 0.5 * (z_m + z_p)              # THE CELL BED THE SCHEME USES
eta_c = h_c + z_bar                    # "free surface"
```

Expanding `z_bar`:

```
z_bar[i] = ( z[i-1] + 2 z[i] + z[i+1] ) / 4  =  z[i] + (1/4) lap(z)[i]
```

so

```
eta_scheme = h + z_bar = eta_true + (1/4) lap(z),     lap(z) = z[i-1] - 2 z[i] + z[i+1]
```

**The lake-at-rest state `eta_true = const` is preserved if and only if
`lap(z) = 0`** — a flat or exactly linear bed. Everywhere else the scheme sees a
free surface that is wrong by exactly one quarter of the bed's discrete
Laplacian, and a spurious gradient drives flow.

Verified to the digit on a 1-D row (`z_bar - z` against `lap(z)/4`):

| bed | `lap(z)/4` at the feature | `eta_scheme` vs flat 155 m |
|---|---|---|
| flat | 0 | 155 everywhere, `dh/dt` **exactly 0** |
| linear slope (interior) | 0 | 155, `dh/dt` **exactly 0** at the interior cell |
| 5 m bump | −2.5 / +1.25 | 152.5 / 156.25 |
| one-cell wall | **−40** / +15 / +25 | **120 / 170 / 180** |
| two-cell wall | −15 / −25 / +15 / +25 | 145 / 135 / 170 / 180 |

This is the smallest reproduction: a 5-cell row, a flat pool, one bed step. It
needs no barrier, no breach and no 2-D geometry.

**This is a true solver defect**, in category (b) *bed averaging* — specifically
the re-derivation of the cell bed as the mean of its two interface values, which
the `_rhs` docstring presents as the well-balanced construction. It is not a
barrier-resolution defect; barrier resolution only controls how large `lap(z)`
gets.

### 45.2 RC-2 — The face bed is the average of the two cells, so a barrier's own height is halved at its upstream face — **CONFIRMED**

Independent of RC-1. At the face between the last reservoir cell (z = 100) and a
one-cell wall (z = 160), the single-valued interface bed is `0.5(100+160) = 130`,
i.e. **30 m below the crest the water is supposed to be held by**.

Full flux trace at that face, pool at 155 m (5 m *below* the crest, so the true
answer is zero flux):

| combination | eta at the cell | face bed | fictitious `hL` | gravity wave speed |
|---|---|---|---|---|
| **both errors (as built)** | 170.0 | 130.0 | **40.0 m** | **19.81 m/s** |
| RC-1 fixed only | 155.0 | 130.0 | 25.0 m | 15.66 m/s |
| RC-2 fixed only | 170.0 | 160.0 | 10.0 m | 9.90 m/s |
| both fixed | 155.0 | 160.0 | **0.0 m** | **0.00 m/s** |

19.81 m/s against the **21.1 m/s** measured in the bounded-lake experiment. The
two errors are **both necessary and neither is sufficient to remove** — RC-2
contributes the larger share (25 m of the 40 m), RC-1 the smaller (10 m).

Answering the question posed in the brief: the cause is **(b) bed averaging and
(c) hydrostatic reconstruction acting together**, not (a), (d), (e) or (f).
Face reconstruction (a) and the Rusanov momentum flux (d) operate correctly on
the states they are given; they are given wrong states. Ghost/boundary treatment
(e) is a real but separate and much smaller effect (see 45.6). Barrier
rasterisation (f) is an **amplifier, not a cause** — it sets the magnitude of
`lap(z)`, which is why the failure is resolution-sensitive but never
resolution-free.

### 45.3 RC-3 — A barrier holds iff two adjacent cells share the crest — **CONFIRMED**

Direct consequence of RC-2. Face-by-face `f_h` (m²/s), pool 5 m below a 160 m
crest, dx = 25 m:

| wall thickness | face beds across the wall | mass flux at each face | outcome |
|---|---|---|---|
| 1 cell | 100, **130**, **110**, 60 | 396.2, −917.2 | **breached** — no face is ever at 160 |
| 2 cells | 100, 130, **160**, 110 | 396.2, **0.0**, −917.2 | **holds** — the wall-to-wall face is at the true crest |
| 3 cells | 100, 130, **160**, **160** | 396.2, **0.0**, **0.0** | holds, with margin |

Confirms and explains the Part IV measurement (one-cell wall passes 46.7 % of
the pool in 300 s; two-cell wall passes 0.000 m³). **One-cell structure
representation is fundamentally insufficient for this scheme** — not marginal,
not a tolerance question. A one-cell barrier has no face anywhere in the domain
at its own crest elevation and therefore does not exist to the solver.

Note the flanking fluxes (396.2 and −917.2 m²/s) persist even for the 2- and
3-cell walls. They do not breach the barrier, but they push water onto dry
barrier cells and pool it there. That is a real accuracy and mass-attribution
defect that survives any thickness.

### 45.4 RC-4 — The reconstruction error is O(bed step), only first-order in dx, and catastrophic at production coarsening — **CONFIRMED**

`|z_bar − z| = |lap(z)|/4` measured over the conditioned DEM (the array the
solver integrates), interior cells only:

| scenario | coarsen | dx (m) | mean (m) | median (m) | p99 (m) | cells > 1 m | cells > 10 m |
|---|---|---|---|---|---|---|---|
| phutkal | 1 | 27.9 | 3.84 | 0.85 | 8.91 | 44.8 % | 0.85 % |
| phutkal | 2 | 55.8 | 8.79 | 2.81 | 248 | 76.3 % | 11.0 % |
| phutkal | **4** | **111.6** | **20.06** | **8.34** | 348 | **90.4 %** | **43.7 %** |
| phutkal | 8 | 223.3 | 42.44 | 23.38 | 426 | 96.8 % | 74.5 % |
| rishiganga | 1 | 28.3 | 6.86 | 1.23 | 10.3 | 57.5 % | 1.03 % |
| rishiganga | 2 | 56.7 | 15.00 | 4.01 | 492 | 85.1 % | 15.3 % |
| rishiganga | **4** | **113.4** | **30.06** | **11.12** | 791 | **94.6 %** | **54.0 %** |
| rishiganga | 8 | 226.8 | 52.28 | 27.58 | 842 | 98.0 % | 79.9 % |

It **converges** — roughly halving as dx halves, i.e. first order — so it is a
legitimate discretisation error, not an outright coding bug. But:

- at the finest resolution the data supports (28 m) the **mean fictitious
  free-surface error is still 3.8–6.9 m**, with ~1 % of cells above 10 m;
- at **coarsen 4, the production default**, the mean is **20–30 m and more than
  half of all cells exceed 10 m**.

A flood model whose reconstructed water surface carries a 20–30 m mean error
before any water moves cannot support depth-based consequence assessment. This
is the single most important number in this Part.

### 45.5 RC-5 — Two of three envelope arms still inject, and still double-count — **CONFIRMED**

P3 converted only the **central** arm. `run_pipeline.py:1132` dispatches the
side arms to `_solve_envelope_arm`, whose payload (`run_pipeline.py:156–173`)
carries `hydrograph_Q_m3s = hg.Q_m3s` **and** `initial_depth = initial_depth`,
and carries no `breach_opening`. So for the pessimistic and optimistic arms the
impoundment is still supplied twice, exactly as defect C1 described.

`envelope_grid = np.maximum(...)` over all three arms feeds `envelope.tif` and
`envelope.geojson`. **The envelope product still contains the 2.00× double
count** even though the central arm no longer does. The validity gates read the
central arm only, so G1 does not see this.

### 45.6 RC-6 — `mode="edge"` padding manufactures a bed kink at the domain rim — **CONFIRMED, minor**

`np.pad(z, 2, mode="edge")` replicates the edge value, so the ghost bed is flat
where the real bed is sloping. On a linear bed this makes `lap(z) ≠ 0` in the
first two real cells — reproduced: a strictly linear bed gives `dh/dt` exactly
0 at the interior cell and **28.4 / −17.5 m/s** at the cells adjacent to the
padding. Same mechanism as RC-1, applied to an artefact of the boundary
treatment. Small in absolute terms against RC-1/RC-2 and only at the rim, but it
is why "linear slope" does not test as exactly balanced end-to-end.

---

## 46. B — Evidence and reproduction results

All reproduced this session; scripts were throwaway and are described rather
than committed, since every one is a few lines against the public entry points.

**Bounded lake at rest** (300 s, `manning_n = 0` so friction cannot mask the
imbalance, eta and |v| sampled only over cells wet at *both* ends so a drained
shoreline cannot be mistaken for drift):

| case | eta spread | \|v\|max | retained |
|---|---|---|---|
| the shipped benchmark's own terrain | n/a | n/a | **0.002 %** |
| same lake, walled | 28.0 m | **21.1 m/s** | 98.9 % |
| flat bed, walled 15 m proud, 55 m deep | 35.4 m | 1.8 m/s | 98.2 % |

The first row is the reason `test_lake_at_rest_is_still` passes: its lake leaves
the domain through the transmissive boundary, so there is essentially nothing
left to be at rest and the velocity sample is taken over cells that have dried.
**The benchmark does not test the property it claims to test.**

**Near-breach velocity, current opening implementation** (synthetic wall, 55 m
head, 100 m drop to the apron, 250 m breach over t_f = 120 s, dx = 25 m):

| t (s) | max \|v\| (h > 1 m) | max Fr | location |
|---|---|---|---|
| 60 | 25.20 | 4.32 | first cell below the wall |
| 120 | **30.26** | 4.98 | apron |
| 240 | 26.06 | **6.93** | first cell below the wall |
| 900 | 15.26 | 3.83 | apron |

Free-fall bound √(2gH) with H = 55 m is 32.85 m/s. **A3 passes.** Against the
audit's injection-era 67 m/s (coarsen 4) / 330 m/s (coarsen 2) / Fr ≈ 17, the
opening is a large improvement on identical head.

**Is the jet numerical?** Two controlled sweeps say no.

*Sweep 1 — vary the apron elevation (changes both the physical drop and
`lap(z)` at the toe):*

| apron (m) | drop (m) | `lap/4` at toe | √(2g(H+drop)) | max \|v\| | v / bound |
|---|---|---|---|---|---|
| 160 | 0 | 0.0 | 32.85 | 12.44 | 0.379 |
| 120 | 40 | 10.0 | 43.17 | 19.65 | 0.455 |
| 100 | 60 | 15.0 | 47.50 | 25.32 | 0.533 |
| 60 | 100 | 25.0 | 55.15 | 30.26 | 0.549 |
| 40 | 120 | 30.0 | 58.60 | 32.25 | 0.550 |

`v / bound` **stabilises at ~0.55** — the velocity tracks the physical free-fall
scale with a constant coefficient, which a numerical artefact would not do.

*Sweep 2 — decisive: hold the drop at exactly 160 → 60 m and spread it over more
cells, so the free-fall bound is identical and only `lap(z)` at the toe changes:*

| ramp cells | max `lap/4` on the ramp | max \|v\| (h > 1 m) | max Fr |
|---|---|---|---|
| 1 | 12.50 | 29.78 | 6.27 |
| 2 | 8.33 | 27.94 | 7.22 |
| 4 | 5.00 | 30.40 | 7.21 |
| 8 | 2.78 | 29.94 | 8.75 |
| 16 | 1.47 | 17.84 | 5.40 |

Velocity is flat at ~28–30 m/s while the numerical term falls **4.5×**. The
decline at 16 cells is a 400 m ramp — the flow genuinely has a gentler slope over
its travel distance, not a numerical change. **CONFIRMED: the near-breach
velocity is physically generated by head plus drop. Fr 5–7 is a real
supercritical dam-break jet for this geometry, not a discretisation artefact.**

**The 2.7× measured-vs-routed discharge ratio:**

| samples used | measured peak (m³/s) | routed peak (m³/s) | ratio |
|---|---|---|---|
| all | 550 795 (at t = 0.4 s) | 203 640 | **2.705** |
| t ≥ 5 s | 122 767 | 203 640 | **0.603** |
| t ≥ 30 / 60 / 120 s | 122 767 | 203 640 | 0.603 |

**CONFIRMED: the 2.7× is entirely the first ~5 seconds** — a storage-change / dt
differencing artefact over sub-second startup timesteps. The sustained ratio is
**0.603**, i.e. the 2D opening passes *less* than the 0-D weir predicts, which is
the physically expected direction: the weir formula assumes a full free overfall
with a static reservoir head, no approach loss and no lateral contraction. The
disagreement is a finding, not a calibration target, and its sign now makes
sense.

**Outlet, rishiganga coarsen 4, real terrain:**

| measurement | before outlet | after outlet |
|---|---|---|
| escape from downstream seed, cell-max | 3234.86 m (**head +1123.25 m**) | 2139.97 m (**head +28.37 m**) |
| escape from downstream seed, interface | 3215.40 m (head +1103.80 m) | 2124.77 m (**head +13.16 m**) |

The outlet works: it removed ~98 % of the escape-head requirement. But:

- the boundary ring is **820 cells and exactly ONE is below 2000 m** (min
  1649.8 m, **median 6393.5 m** — the `condition_dem` nodata wall);
- the surviving requirement is still **13.2–28.4 m of ponding** at the
  downstream seed before any water can reach that one cell;
- the outlet cells themselves are clean — three contiguous cells all at
  1649.8 m, every face bed also 1649.8 m, **zero interface-averaging penalty**.
  `open_river_outlets` defaults to `width_cells = 1`, and widening it to 2 or 3
  changes the escape level not at all (2175.15 m in every case).

So the outlet is **not** mis-elevated, **not** disconnected, and **not**
suffering the RC-2 penalty. The limiting factor is the residual head plus a
single-cell exit aperture.

**Regime classification** (`src/m3_breach/__init__.py`): rishiganga
`debris_flow` → `applicable = False`, `approximate = False`, 8 missing closures;
phutkal `hyperconcentrated` → `approximate = True`, same 8 listed.

---

## 47. C — Root-cause dependency chain

```
RC-1  cell bed re-derived as z_bar = z + lap(z)/4
  │     (eta_scheme = eta_true + lap(z)/4 ; balanced only where lap(z) = 0)
  │
RC-2  face bed = mean of the two cells
  │     (a barrier's crest is averaged away at its own upstream face)
  │
  ├──► RC-3  a one-cell barrier has NO face at its crest  ──► the barrier does
  │          not exist to the solver; 46.7 % of the pool crosses it in 300 s
  │
  ├──► bounded lake at rest fails: 40 m of fictitious depth at the barrier face
  │    ──► 19.8 m/s predicted, 21.1 m/s measured
  │          │
  │          ├──► P2 acceptance ("the pool holds with Q = 0") CANNOT pass,
  │          │    for reasons upstream of the barrier, the breach and the IC
  │          │
  │          └──► contaminates the first seconds of the measured breach Q
  │               ──► the 2.705 peak ratio (dissolves to 0.603 after t = 5 s)
  │
  └──► RC-4  error is O(bed step), first order in dx
             ──► mean 3.8-6.9 m at 28 m; 20-30 m at production coarsen 4
             ──► refinement within the available data does NOT remove it

INDEPENDENT of the above:

RC-5  envelope side arms never converted to the opening
      ──► envelope.tif / envelope.geojson still carry the 2.00x C1 double count

RC-6  mode="edge" padding manufactures lap(z) != 0 at the domain rim (minor)

G4 retention failures (rishiganga 12.79 m, phutkal 15.04 m over) are
SEPARATE from all of the above: they are scenario water levels set above what
the basin holds, and they would remain after RC-1..RC-4 were fixed.

The near-breach velocity (30 m/s, Fr 5-7) is NOT on this chain. It is physical.
```

---

## 48. D — What is actually fixed vs still open

### C1 — reservoir water counted twice

**Fixed on the central arm, by construction.** Live phutkal coarsen 4:
`supplied_m3 = 2.9675e6`, identical to `initial_pool_volume_m3`;
`volume_injected_m3 = 0`. `_boundaries` is emptied when an opening is present,
and supplying both raises `ValueError`.

**Still open on the envelope arms** — RC-5. Two of three arms inject on top of
`initial_depth`. The envelope raster is contaminated; the central-arm products
are not.

### C2 — breach injected into the pool at its deepest cell

**Fixed.** The Gaussian source is gone from the central arm. The opening is cut
over `breach_mask & barrier_mask`, i.e. on the structure, not at the pool floor.
`snap_to_thalweg` no longer positions the release.

### C3 — no barrier in the solver's terrain

**Fixed as emplacement, defeated as physics.** `dem_elev` genuinely carries the
structure at a sourced crest (measured on phutkal coarsen 4: 2 of 4 barrier
cells raised, max raise 49.79 m, 8.24e5 m³ of material). But by RC-2/RC-3 a
barrier thinner than two cells is invisible to the flux operator, and at
coarsen 4 phutkal's barrier is **4 cells total** and the opening is **1 cell**.
The terrain has a dam; the solver does not.

### Is the measured Q genuinely emergent?

**Yes, for the central arm — CONFIRMED.** With `breach_opening` set,
`_boundaries` is empty, so no source term exists anywhere in the domain. The
control volume (`upstream_basin_mask | barrier_mask`) is fed only by the initial
pool. Its storage derivative, corrected for bed-lowering re-attribution, is
therefore the flux across its own boundary and nothing else. The integral closes
to within 5 %.

Two qualifications, both measured: the **first sample** is a sub-second startup
artefact (550 795 vs a 122 767 sustained peak) and is suppressed at `step == 0`
but not beyond it; and the early samples inherit the RC-1/RC-2 spurious motion,
which is why only the integral, not the instantaneous trace, is currently
defensible.

### Hidden double counting or duplicated state anywhere else?

Searched. `vol_injected` accumulates at exactly one site
(`swe_2d.py:966`), reachable only through `_boundaries`, which the opening
empties. `initial_depth` is produced once by `compute_lake_depth_grids` and is
the same array the control volume measures. `volume_bed_lowering_m3` is a
distinct counter and is not folded into `clipped`. **No further duplication
found on the central path.** The one remaining duplication is RC-5.

---

## 49. E — Candidate fixes, ranked by physical correctness and numerical risk

Ranked; **none implemented**. Each names what it would break.

### E-1 — Positivity-preserving hydrostatic reconstruction with a single-valued interface bed taken as the MAX, not the mean (Audusse et al. 2004, as actually published)

Use `z_face = max(z_L, z_R)` and reconstruct `h*_L = max(0, eta_L − z_face)`,
`h*_R = max(0, eta_R − z_face)`, with the standard bed-slope source split that
restores conservation. This is the scheme the `_rhs` docstring already cites by
name; what is implemented is a mean-bed variant of it.

- **Physical correctness: highest.** Removes RC-2 completely and RC-3 with it: a
  one-cell barrier keeps its full crest at both faces. Exactly preserves lake at
  rest over arbitrary bed topography, which is the defining property.
- **Numerical risk: moderate.** Changes every flux in the solver. Requires the
  bed-slope source term to be rewritten consistently or conservation is lost.
  Ritter, mass closure and every archived comparison must be re-run.
- Does **not** on its own fix RC-1; see E-2, which it composes with.

### E-2 — Reconstruct the free surface against the cell's own bed `z`, not `z_bar`

`eta_c = h_c + z[i]`. Removes RC-1 exactly; the lake-at-rest state becomes
`eta_c = const` by definition rather than by coincidence of a vanishing
Laplacian.

- **Physical correctness: high.** Restores the identity the whole construction
  is meant to rest on.
- **Numerical risk: moderate–high.** `z_bar` is currently what makes the
  interface beds and the cell bed mutually consistent; changing one without the
  other is what produced the present mismatch. **E-1 and E-2 must land
  together** — either alone still leaves 10–25 m of fictitious face depth at a
  barrier (table in 45.2).

### E-3 — Enforce a minimum structural thickness at the geometry gate

Refuse any run whose barrier or opening is under two cells thick along the spill
path, at the resolution actually requested — `escape_head_4connected(...,
interface_bed=True)` already measures exactly this and gate G4 already reports
it.

- **Physical correctness: neutral.** Changes no physics; prevents a
  configuration the scheme provably cannot represent.
- **Numerical risk: none.** Pure gating.
- **Caveat: this is containment, not a fix.** It would close the remaining
  scenarios rather than make them run, and it leaves RC-1/RC-4 untouched
  everywhere else in the domain. Cheap and honest as an interim.

### E-4 — Convert the envelope side arms to openings

Removes RC-5 and the last double count. Mechanically the same change already
made for the central arm.

- **Physical correctness: high** (it removes a known-wrong path).
- **Numerical risk: low.** Isolated to the payload builder and
  `_solve_envelope_arm`. The arms would need their own `BreachParams` to drive
  per-arm width and formation time.
- Should follow E-1/E-2, or all three arms inherit the same defect.

### E-5 — Sub-grid barrier representation (porosity / partial-face blocking)

Carry a per-face blocking fraction and crest elevation independent of cell size,
so a structure narrower than a cell still blocks.

- **Physical correctness: high** for the real problem — a 50 m gorge and a 30 m
  dam in a 28–113 m cell.
- **Numerical risk: high.** A substantial extension, new state per face, new
  calibration surface. Only worth it after E-1/E-2, and only if refinement is
  ruled out.

### E-6 — Reflective (rather than zero-gradient) ghost cells at the domain rim

Removes RC-6.

- **Physical correctness: low priority**; RC-6 is minor and confined to the rim.
- **Numerical risk: low**, but it would change the outlet's behaviour, which
  currently *relies* on the transmissive boundary to let water leave. **Do not
  do this before the outlet is understood as a rating boundary** (see G).

### Rejected outright

Smoothing the DEM to reduce `lap(z)`; raising barriers above their sourced crest
to survive averaging; capping velocity or Froude; scaling measured Q toward the
routed value; widening the outlet until outflow appears. Each suppresses a
symptom this Part has traced to a cause, and each is banned by §41.

---

## 50. F — Tests and acceptance criteria required before implementation

Ordered so that each is the instrument for the next.

**F-1 — Lake at rest on arbitrary topography (the real benchmark).** Replace the
current pass condition. A bounded lake over (a) flat, (b) linear, (c) a
one-cell bump, (d) a one-cell wall, (e) a one-cell notch, and (f) real
conditioned DEM terrain, all with `manning_n = 0`:

- retained volume > 99.9 %;
- free-surface spread over cells wet at both ends < 1e-6 m;
- max |v| < 1e-6 m/s.

**The existing `test_lake_at_rest_is_still` must be fixed or deleted**: its lake
retains 0.002 % and it cannot fail. Keep
`tests/test_lake_at_rest_bounded.py`'s strict `xfail` as the signal.

**F-2 — Barrier integrity at one-cell thickness.** The one-cell wall case must
pass 0.000 m³ for 900 s with the pool 5 m below the crest, and identically at
2, 3 and 5 cells. Current: 46.7 % at one cell.

**F-3 — Direct assertion on the reconstruction error.** `|z_bar − z| < 1e-9`
over the whole grid, or whatever the replacement's equivalent identity is. This
pins RC-1 at the source instead of inferring it from a run.

**F-4 — Ritter, mass closure and well-balanced re-run together.** E-1/E-2 touch
every flux. Ritter RMSE must not regress from 0.043 m; `mass_closure()` relative
error must stay < 0.01; the front-position check must not degrade.

**F-5 — Grid convergence with the fix in.** Repeat the 45.4 table. Mean
`|z_bar − z|` equivalent must be zero at every coarsening, not merely smaller.
Depth and arrival fields should then converge under refinement; verify they do.

**F-6 — P2 acceptance, retried.** Only meaningful after F-1/F-2: with Q = 0 the
pool holds, on a scenario whose water level its basin actually retains. This
needs a G4-clean scenario, which does not exist today.

**F-7 — A2/A3/A4 on real terrain.** Superelevation ≤ a few metres, |v| below
√(2gH), and the measured/routed ratio re-measured. The present 0.603 is the
reference to beat; a change in it after E-1/E-2 is the signal that the earlier
value was contaminated.

**F-8 — Envelope arms carry no injection.** `volume_injected_m3 == 0` on all
three arms, and the envelope's supplied volume equals the impoundment.

---

## 51. G — Explicitly must NOT be changed

- **Part II's river work stays.** The outlet, flowline conditioning, channel
  roughness and the regime gate are upstream of everything here and are not
  implicated by any measurement in this Part.
- **The outlet must not be widened, lowered, or re-elevated on the strength of
  the 4.774e-03 m³ result.** Measured: the outlet cells are clean, carry no
  interface-averaging penalty, and `width_cells` 1 → 2 → 3 changes the escape
  level not at all. The limiter is 13.2–28.4 m of residual ponding and a
  single open boundary cell out of 820. Changing the outlet would be treating
  the wrong object.
- **Do not tune the breach growth law to close the measured-vs-routed gap.**
  §41 already bans it; this Part strengthens the case — the sustained ratio is
  0.603 and points the physically expected way.
- **Do not cap or clamp velocity, Froude, discharge or momentum.** The jet is
  confirmed physical (two independent sweeps).
- **Do not smooth the DEM to reduce `lap(z)`.** That fabricates terrain to suit
  a discretisation.
- **Do not relax G1–G4.** G4's retention failures are scenario data, not gate
  error.
- **Do not "fix" `test_lake_at_rest_is_still` by loosening its tolerance.** It
  needs a terrain where the lake stays in the domain; its tolerance was never
  the problem.
- **Do not change `mode="edge"` padding before the outlet's boundary role is
  settled** — the outlet currently depends on the transmissive boundary.
- **Keep the 0-D routed hydrograph.** It is now the only independent check on
  the measured discharge.

---

## 52. H — Recommended implementation order

Each step is the instrument for the next; the ordering is not stylistic.

1. **F-1 and F-3 first, as failing tests.** Write the real lake-at-rest
   benchmark and the direct `z_bar` identity assertion *before* touching the
   scheme. Without them there is no way to tell a fix from a coincidence, and
   the existing benchmark demonstrably cannot fail.
2. **E-1 + E-2 together, as one change.** Interface bed as `max`, free surface
   against the cell's own bed, bed-slope source rewritten to match. They cannot
   be split — the 45.2 table shows either alone still leaves 10–25 m of
   fictitious depth.
3. **F-4 immediately after.** Ritter, mass closure, front position. If any
   regresses, stop; the fix is wrong.
4. **F-2, then F-5.** Barrier integrity at one cell, then the grid-convergence
   table re-measured. Expect the 20–30 m coarsen-4 error to vanish, not shrink.
5. **E-4** — convert the envelope arms. Only now, so all three arms get the
   corrected scheme.
6. **Re-measure everything Parts I–IV asserted about depth, velocity and
   superelevation.** Every one of those numbers was taken on a scheme carrying a
   20–30 m mean free-surface error at production resolution. Treat them as
   provisional until re-run, including the ones this Part quotes.
7. **Only then revisit the scenarios.** Fix the G4 water levels against sourced
   data, and re-attempt P2's acceptance (F-6) and A2/A3/A4 on real terrain
   (F-7).
8. **E-3 as containment throughout**, if any scenario must be runnable before
   step 2 lands — but recorded as containment, never as a fix.
9. **E-5 only if** step 4 shows that refinement to 28 m still cannot represent
   these gorges, and **E-6 only after** the outlet is reconsidered as a rating
   boundary rather than a hole.

---

## 53. Debris regime — what would be required, and whether SWE can stand in

**No implementation. Determination only.**

`src/m3_breach/__init__.py::DEBRIS_FLOW_MISSING_PHYSICS` enumerates eight
closures. Confirmed against the solver: **all eight are absent from
`swe_2d.py`** — it carries `h`, `hu`, `hv`, a fixed `z` (now mutable only over
the breach opening), and a single scalar or gridded Manning `n`. There is no
concentration field, no density variable, no yield stress, no entrainment term,
no Exner equation, no grain-size state, no impact-pressure output.

Required to represent a Chamoli / Rishi Ganga type event:

1. solid volumetric concentration `c(x,y,t)` as a transported state;
2. mixture density `ρ_m = ρ_w(1−c) + ρ_s c` entering both mass and momentum;
3. a non-Newtonian resistance closure — Voellmy (μ, ξ) or Bingham /
   Herschel–Bulkley (τ_y, plastic viscosity) — replacing the single Manning `n`;
4. a bed entrainment/deposition law `E(x,y,t)`;
5. an Exner-type bed-evolution equation so scour and deposition change the
   routed terrain;
6. grain-size-dependent settling and phase separation for the coarse fraction;
7. a momentum pressure term using `ρ_m`, not `ρ_w`;
8. impact pressure `ρ_m v²` for exposure, which is currently proxied from depth.

**Can clear-water SWE legitimately represent that event? No — CONFIRMED, and the
code already says so.** `parse_flow_regime('debris_flow')` →
`applicable = False, approximate = False`, and the validity gate refuses the run
with `DEBRIS_FLOW_MISSING_PHYSICS`. That is the correct behaviour and must not be
softened. A ~27 Mm³ rock-and-ice avalanche is a different system of equations,
not a calibration offset: no choice of `n`, and no multiplier on Q, recovers a
yield stress, an entrained bed, or a mixture density.

`hyperconcentrated` (phutkal) is treated differently and correctly — it proceeds
with `approximate = True` and a stated bias (depth and momentum read low),
because at those concentrations the mixture is still near-Newtonian.

**Consequence for scope:** rishiganga cannot be presented as a modelled event
under this solver at any resolution, independent of every other defect in this
Part. It is a refusal, not a backlog item.

---

## 54. One-line state of the model after Part V

> The release is now real — the impoundment is supplied once, the breach is cut
> in the structure, and Q is measured rather than prescribed. What sits under it
> is a flux operator that is well-balanced only where the bed's discrete
> Laplacian vanishes, which at production resolution means a **20–30 m mean
> error in the reconstructed water surface before any water moves**.

---
---

# Part VI — Independent conformance audit and reproduction of Part V

**Date:** 2026-09-13.
**Status:** **AUDIT AND PLAN ONLY. ZERO PRODUCTION CODE CHANGES IN THIS PASS.**
Diagnostic scripts were written to a session scratchpad outside the repository;
nothing in `src/`, `tests/`, `run_pipeline.py` or `frontend/` was modified.
`ATLAS.md` was regenerated (§58, Sweep D2) and came back byte-identical.

Every number below was produced by a command in this session against the working
tree at `git HEAD d903da9` plus the uncommitted changes present on 2026-09-13.
Where a number is quoted from an earlier Part rather than re-measured, it says so.

Confidence labels follow §7 of the brief: **CONFIRMED EXISTING KNOWLEDGE** ·
**NEW CONFIRMED FACT** · **HYPOTHESIS** · **CONTRADICTION** · **STALE
DOCUMENTATION**.

---

## 55. A — Reproduction results: Part V confirmed, corrected, or overturned

### 55.1 RC-1 — **CONFIRMED, to the digit**

`src/m4_solvers/swe_2d.py:590–594` is unchanged and builds the cell bed exactly
as Part V describes. The identity was checked directly rather than inferred:

```
scratchpad/rc1_rc2_rc6.py   — calls swe_2d._rhs and the z_bar construction on 1-D rows
```

| bed | `lap(z)/4` | `z_bar − z` | residual | `eta_scheme` (pool 155 m) |
|---|---|---|---|---|
| flat `[100,100,100,100,100]` | `[0,0,0,0,0]` | `[0,0,0,0,0]` | **0.000e+00** | `[155,155,155,155,155]` |
| linear `[140,130,120,110,100]` | `[−2.5,0,0,0,2.5]` | same | **0.000e+00** | `[152.5,155,155,155,157.5]` |
| 5 m bump `[100,100,105,100,100]` | `[0,1.25,−2.5,1.25,0]` | same | **0.000e+00** | `[155,156.25,152.5,156.25,155]` |
| one-cell wall `[100,100,160,60,60]` | `[0,15,−40,25,0]` | same | **0.000e+00** | `[155,170,120,180,155]` |
| two-cell wall `[100,100,160,160,60]` | `[0,15,−15,−25,25]` | same | **0.000e+00** | `[155,170,145,135,180]` |

`max |(z_bar − z) − lap(z)/4| = 0.000e+00` on every row. Part V's own table
(§45.1) is reproduced value for value, including the −40/+15/+25 and
120/170/180 entries.

Driving `_rhs` directly on the 2-D tiling of each row, with the pool at rest and
`manning_n` irrelevant (this is the spatial operator, before friction):

| bed | `dh/dt` (mid row) | `max\|dh/dt\|` |
|---|---|---|
| flat | `[0,0,0,0,0]` | **0.0000e+00** |
| linear | `[1.3069,−0.7004,0,1.1347,−2.3222]` | 2.3222e+00 |
| 5 m bump | `[0.5873,−2.3095,3.4444,−2.3095,0.5873]` | 3.4444e+00 |
| one-cell wall | `[7.8615,−23.7088,52.5342,−53.8421,17.1552]` | **5.3842e+01** |

Flat is **exactly** zero. Linear is exactly zero at every interior cell and
non-zero only at the two cells adjacent to the padding — which is RC-6, not
RC-1. **Confirmed.**

### 55.2 RC-2 — **CONFIRMED, to the digit**

Same script. Flux decomposition at the face between the last reservoir cell
(z = 100) and a one-cell wall (z = 160), pool at 155 m — 5 m **below** the crest,
so the correct answer is zero flux:

```
interface bed between cell(z=100) and cell(z=160) : 130.0   (true crest 160.0)
```

| combination | `eta` at the cell | face bed | fictitious `hL` | `sqrt(g·h)` |
|---|---|---|---|---|
| **both errors (as built)** | 170.00 | 130.00 | **40.00** | **19.81** |
| RC-1 fixed only | 155.00 | 130.00 | 25.00 | 15.66 |
| RC-2 fixed only | 170.00 | 160.00 | 10.00 | 9.90 |
| both fixed | 155.00 | 160.00 | **0.00** | **0.00** |

Every cell of §45.2's table reproduces exactly. Neither fix alone removes the
fictitious depth; **E-1 and E-2 must land together**. **Confirmed.**

### 55.3 RC-3 — **CONFIRMED; two published numbers corrected**

`scratchpad/rc3_barrier.py`. Face beds across the wall, pool 155 m, crest 160 m,
dx = 25 m:

| thickness | cell beds | face beds | any face at 160? |
|---|---|---|---|
| 1 | `100 100 160 60 60` | `100 130 110 60` | **False** |
| 2 | `100 100 160 160 60 60` | `100 130 160 110 60` | True |
| 3 | `100 100 160 160 160 60 60` | `100 130 160 160 110 60` | True |

Exactly §45.3. A one-cell wall has **no face anywhere at its own crest**.

300 s run, `manning_n = 0`, pool sealed on its other three sides so the only exit
is across the wall:

| thickness | pool V₀ (m³) | on the apron at t=300 s (m³) | retained in domain |
|---|---|---|---|
| **1** | 5.5000e+06 | 5.3462e+03 | **44.601 %** |
| 2 | 5.5000e+06 | 7.9596e+01 | 98.322 % |
| 3 | 5.5000e+06 | 1.2106e+02 | 98.734 % |
| 5 | 5.5000e+06 | 1.3448e+02 | 98.746 % |

**Correction to §45.3 and to `INVARIANTS.md` §2.** Both state "a one-cell wall
passes 46.7 % of the pool in 300 s; 0.000 m³ passes a two-cell wall." On this
fixture a one-cell wall passes **55.4 %** (100 − 44.601 — the pool is sealed
elsewhere, so everything that left crossed the wall), and a two-cell wall passes
**7.96e+01 m³**, i.e. 1.4e-5 of the pool — very small, but **not identically
zero**. The qualitative invariant is unchanged and if anything stronger than
stated. The two quoted figures are fixture-specific and must not be quoted as
constants; neither fixture is wrong, the numbers simply do not travel.

### 55.4 RC-4 — **CONFIRMED EXACTLY, and the metric is now pinned**

Part V did not state which aggregation of `|z_bar − z|` its table used, and the
plausible readings disagree by 2×. `scratchpad/rc4_variants.py` computed all five
against the conditioned DEM. Exactly one reproduces §45.4:

| variant | phutkal c=1 mean | phutkal c=4 mean | rishiganga c=4 mean |
|---|---|---|---|
| x-axis only | 2.09 | 10.40 | 15.72 |
| max(\|ex\|,\|ey\|) | 3.58 | 17.61 | 27.12 |
| mean(\|ex\|,\|ey\|) | 2.02 | 10.73 | 15.94 |
| **\|ex + ey\| (5-point lap/4)** | **3.84** | **20.06** | **30.06** |
| \|ex\|+\|ey\| | 4.05 | 21.45 | 31.88 |

The `|ex + ey|` row matches §45.4 in **every** cell — mean, median, p99,
`>1 m %` and `>10 m %`, at both coarsenings, for both scenarios:

| scenario | coarsen | dx (m) | mean | median | p99 | >1 m | >10 m |
|---|---|---|---|---|---|---|---|
| phutkal | 1 | 27.9 | 3.84 | 0.85 | 8.9 | 44.8 % | 0.85 % |
| phutkal | 4 | 111.6 | 20.06 | 8.34 | 347.6 | 90.4 % | 43.66 % |
| rishiganga | 1 | 28.3 | 6.86 | 1.23 | 10.3 | 57.5 % | 1.03 % |
| rishiganga | 4 | 113.4 | 30.06 | 11.12 | 791.1 | 94.6 % | 54.01 % |

**NEW CONFIRMED FACT — a correction to how the headline should be read.**
`|ex + ey|` is the **sum of the two directional bed errors**. The scheme never
forms that sum: `_rhs` loops over axes and applies `ex` only to the x-flux and
`ey` only to the y-flux, against two different `eta_c` arrays. There is no single
"reconstructed free surface" in this scheme carrying a 20–30 m error. The error
each flux actually sees is the per-axis figure: **10.40 m (phutkal) and 15.72 m
(rishiganga) mean at coarsen 4**, and 2.09 / 3.45 m at coarsen 1.

This changes nothing about severity — a 10–16 m mean bed-reconstruction error
under a 58 m impoundment is still disqualifying for depth-based consequence
assessment — but "20–30 m mean free-surface error" is an aggregate over two
directions and should be stated as such wherever it is quoted, including
`INVARIANTS.md` §2 and `memory.md`.

### 55.5 RC-5 — **CONFIRMED by source, unchanged**

`run_pipeline.py:156–180` (`_solve_envelope_arm`) passes
`hydrograph_Q_m3s=payload["Q"]` **and** `initial_depth=payload.get("initial_depth")`
and never passes `breach_opening`. The payload built at `run_pipeline.py:1114–1127`
carries both. `run_pipeline.py:1132` dispatches every non-central arm through it.
`envelope_grid = np.maximum(...)` at lines 1246 and 1255 folds all three arms into
`envelope.tif` and `envelope.geojson` (written at 1303–1311), registered as
artifacts (1950–1953) and served by `src/api/main.py:481` / `:493`.

The C1 double count is therefore still live in the shipped envelope products.
**Confirmed; no change since Part V.**

### 55.6 RC-6 — **mechanism CONFIRMED, magnitude not reproducible**

A strictly linear bed at rest, `manning_n = 0`, dx = 25 m, pool 155 m:

```
z          [140. 130. 120. 110. 100.  90.  80.]
dh/dt      [ 1.306884 -0.700357  0.  0.  0.  1.33344  -2.712094]
interior cells 2..4      max|dh/dt| = 0.000e+00
rim cells (0,1,-2,-1)    dh/dt = 1.3069e+00  -7.0036e-01  1.3334e+00  -2.7121e+00
```

The mechanism is exactly as §45.6 states: `mode="edge"` replicates the edge value,
so the ghost bed is flat where the real bed slopes, `lap(z) ≠ 0` appears in the
first two real cells, and the interior is exactly balanced. Part V quotes
"28.4 / −17.5 m/s" at the padded cells; this fixture gives ~1.3 / −2.7 m/s of
`dh/dt`. **The setup behind Part V's figures is not recorded** (§46 states the
scripts were throwaway), so the magnitude could not be checked. Treat 28.4 / −17.5
as fixture-specific and unverified. The mechanism is confirmed and remains minor
relative to RC-1/RC-2.

### 55.7 The jet — **CONFIRMED PHYSICAL, and the bound used to test it is wrong**

Reproduced on the A3 fixture (`tests/test_breach_opening.py::_terrain`,
`manning_n = 0.03` — the geometry and friction Part V used).

**Sweep 1 — vary the apron elevation.** Reproduces §46 exactly:

| apron | `lap/4` at toe | Part V bound | **true bound** | max \|v\| | v / Part V | **v / true** | max Fr |
|---|---|---|---|---|---|---|---|
| 160 | 0.00 | 32.85 | — | 12.44 | 0.379 | — | 3.54 |
| 120 | 10.00 | 43.17 | 26.20 | 19.65 | 0.455 | 0.750 | 4.60 |
| 100 | 15.00 | 47.50 | 32.85 | 25.32 | 0.533 | 0.771 | 5.83 |
| 60 | 25.00 | 55.15 | 43.17 | **30.26** | 0.549 | 0.701 | **6.93** |
| 40 | 30.00 | 58.60 | 47.50 | 32.25 | 0.550 | 0.679 | 6.56 |

**Sweep 2 — the decisive one: hold the 160 → 60 m drop and spread it over more
cells, so the free-fall bound is identical and only `lap(z)` changes:**

| ramp cells | ramp length | max `lap/4` | max \|v\| | max Fr |
|---|---|---|---|---|
| 1 | 25 m | 25.00 | 30.26 | 6.93 |
| 2 | 50 m | 12.50 | 30.05 | 6.27 |
| 4 | 100 m | 6.25 | 30.06 | 6.85 |
| 8 | 200 m | 3.12 | 31.36 | 8.78 |
| 16 | 400 m | 1.56 | 17.80 | 5.39 |

Velocity is flat at 30.0–31.4 m/s while the numerical term falls **16×**
(25.00 → 1.56). Part V measured a 4.5× reduction over the same span; this run
spans 16× and the velocity still does not move. **The near-breach jet is
physically generated by head plus drop — CONFIRMED, with stronger evidence than
Part V had.** The fall at 16 cells is a 400 m ramp: a genuinely gentler slope over
the travel distance, not a numerical change.

**CONTRADICTION — the free-fall bound both Part V and test A3 use is not the
physical bound, and they are wrong in opposite directions.**

`tests/test_breach_opening.py:206` computes `head = 155.0 - FLOOR` = **55 m**, the
pool depth above the *reservoir floor*, and calls `sqrt(2·g·55) = 32.85 m/s` the
free-fall bound. But the water falls from the pool surface (155 m) to the apron
(60 m) — a fall of **95 m** — so the free-fall bound is
`sqrt(2·g·95) = 43.17 m/s`. The test's bound is **1.31× too tight**.

Part V's sweep-1 column makes the opposite error: `sqrt(2g(H + drop))` adds the
pool depth (55 m) to the crest-to-apron drop (100 m) = 155 m, but the pool floor
is at 100 m and the apron at 60 m, so the real fall is 95 m, not 155 m. Part V's
denominator is **1.47× too generous**, which is why "v/bound ≈ 0.55, stable"
comes out so low. Against the correct fall the coefficient is **0.68–0.77** —
still stable, still below 1, so the *conclusion* survives intact; the *number*
does not and must not be quoted as 0.55.

Measured consequence, same fixture, friction varied (`scratchpad/a3_bound.py`):

| `manning_n` | max \|v\| | v / test's bound | v / true bound | v / Ritter `2√(g h₀)` | max Fr | A3 verdict |
|---|---|---|---|---|---|---|
| 0.030 | 30.26 | 0.921 | 0.701 | 0.651 | 6.93 | PASS |
| 0.010 | 33.15 | 1.009 | 0.768 | 0.714 | 7.33 | PASS (inside the 1.05 margin) |
| **0.000** | **34.73** | **1.057** | 0.804 | 0.748 | 8.90 | **FAIL** |

**NEW CONFIRMED FACT (N-13).**
`test_a3_velocity_stays_below_the_free_fall_bound` passes only because the fixture
carries `manning_n = 0.03`. Remove the friction — the honest configuration for a
kinematic bound, since friction can only reduce velocity — and the test **fails
against a bound that is itself 1.31× too tight**. The physics is fine: 34.73 m/s
is comfortably below both the correct free-fall bound (43.17) and the Ritter
dry-bed front speed `2√(g·h₀) = 46.46 m/s`. The *test* is wrong in two
independent ways and currently passes by coincidence.

### 55.8 The measured/routed discharge ratio — **measured side exact; ratio not reproducible**

`scratchpad/q_ratio.py`, A3 fixture, 900 s, opening 250 m over t_f = 120 s:

| samples used | measured peak (m³/s) | t at peak (s) |
|---|---|---|
| all | **550 795** | **0.38** |
| t ≥ 1 s | 199 918 | 1.05 |
| t ≥ 5 s | **122 767** | 126.12 |
| t ≥ 30 / 60 / 120 s | 122 767 | 126.12 |

Both of Part V's measured figures reproduce **to the digit** — 550 795 at
t ≈ 0.4 s, 122 767 sustained. The startup spike is confirmed as a
storage-derivative artefact over sub-second timesteps, dissolving entirely by
t = 5 s.

The measured hydrograph closes: `∫Q dt = 2.7533e+07 m³` against **2.7865e+07 m³**
that actually left the pool — **1.2 % closure**, inside the 5 % §48 claims.

**Part V's routed peak of 203 640 m³/s could not be reproduced.** A 0-D
trapezoidal-weir routing over the same opening geometry and the same drawdown
gives **147 942 m³/s** (peak at t = 120 s). Ratios therefore come out **3.723**
(all samples) and **0.830** (t ≥ 5 s) instead of 2.705 and 0.603. The difference
is entirely in the denominator: 550795/203640 = 2.705 and 122767/203640 = 0.603
confirm Part V's arithmetic, so its routed curve differs from this one by 1.38× —
a routing-implementation difference, not a solver difference.

**Status: the ratio is downgraded from a reported number to a HYPOTHESIS.** The
*direction* is robust and reproduced: the sustained measured discharge is **below**
the 0-D weir prediction (0.830 here, 0.603 there), which is physically expected —
the weir formula assumes a full free overfall with a static reservoir head, no
approach loss and no lateral contraction. The *value* 0.603 is not reproducible
from the repository and must not be quoted as a reference to beat until the
routing that produced it is committed. What settles it: export
`res.breach_q_m3s` alongside the routed arm into `hydrograph.json` (defect **N-1**)
so both curves come from committed code.

### 55.9 The outlet — **partially reproduced**

Part V's rishiganga figures (1123.25 m → 28.37 m escape head, 1 of 820 boundary
cells open, `width_cells` 1→3 changing nothing) were not re-measured; the live run
in §56 is phutkal. What this session measured end to end, phutkal coarsen 4:

```
gate_g3_reachable_outlet: ok=True, escape_head_m = 55.53, threshold_m = 58.0
river_outlets_opened     = 1
outflow_gross_m3         = 0.0
```

G3 passes by **2.47 m** out of 58 m. In 3600 s of simulated time **zero cubic
metres left the domain** — `outflow_gross_m3` is exactly 0.0, and that is the
gross counter which exists specifically to distinguish a sealed domain from one
where inflow and outflow cancel. This is consistent with §46's `4.774e-03 m³` and
independently confirms the domain is effectively sealed at production resolution.
The §51 ban on widening or lowering the outlet stands — nothing measured here
implicates the outlet cells.

---

## 56. B — Current executable state (Sweep C)

### 56.1 C1 — full test suite

```
python -m pytest -q -rf --durations=15
185 passed, 2 xfailed, 259 warnings in 254.35s (0:04:14)
```

**Zero failures, zero errors, zero skips.** Identical to the count Part V records.
Nothing is red because of prior work; nothing was already red. Slowest:
`test_crest_gate_rejects_wse_above_dem_barrier` 112.30 s,
`test_pipeline_emits_validity_and_manifest_lifecycle_completes` 25.25 s.

Note for Sweep D4: `tests/test_swe_gpu.py` **ran** rather than skipping (11.83 s
on `test_gpu_lake_at_rest_stays_at_rest`), so cupy is available on this machine
and the GPU code path is live, not dormant. Zero skips also means
`tests/test_mass_gates.py`'s two `pytest.skip` guards did not fire — the certified
archive run is present.

### 56.2 C2 — geometry gate, every scenario × every coarsening

`scratchpad/c2_gates.py` calls `validate_geometry` directly against each
scenario's manifest and its conditioned, coarsened DEM:

| scenario | c=1 | c=2 | c=4 | c=8 | failure message |
|---|---|---|---|---|---|
| **phutkal** | PASS | PASS | PASS | FAIL | `geometry unresolved at DEM resolution` |
| **rishiganga** | PASS | PASS | PASS | FAIL | `geometry unresolved at DEM resolution` |
| derna | FAIL | FAIL | FAIL | FAIL | `geometry manifest requires a finite sourced crest_elev_m — the barrier cannot be emplaced at an assumed elevation` |
| malpasset | FAIL | FAIL | FAIL | FAIL | `upstream/downstream seeds lack separated connected components` (c=8: `geometry unresolved`) |
| ivanovo | FAIL | FAIL | FAIL | FAIL | `upstream/downstream seeds lack separated connected components` (c=8: `geometry unresolved`) |
| south_lhonak | FAIL | FAIL | FAIL | FAIL | `geometry manifest requires a finite sourced crest_elev_m` |
| **annamayya** | — | — | — | — | **no DEM at all** |

**NEW CONFIRMED FACT.** annamayya no longer reaches the geometry gate:
`get_dem("annamayya", allow_synthetic=False)` raises
`RuntimeError: Could not fetch Copernicus GLO-30 for 'annamayya': cached DEM …` —
the cached tile is gone from the working tree. `memory.md` records annamayya as
failing for *"no river role"*; it now fails one stage earlier, on data
availability. **STALE DOCUMENTATION.**

Two further corrections to that memory entry, both measured above: derna's gate
message is the **crest-sourcing** refusal (the abutment/DEM mismatch is the
*cause*; the gate names the missing `crest_elev_m`), and south_lhonak fails on
crest sourcing rather than on a missing manifest —
`data/geometry/south_lhonak_chungthang.json` and `south_lhonak_moraine.json` both
exist and the first is what the loader picks.

### 56.3 C3 — the one scenario that runs end to end

`scratchpad/c3_run.py`: phutkal, `coarsen=4`, `wse_m=3794.11`,
`total_duration_s=3600`, real DEM. Completed; wrote 16 artifacts.

```json
"valid": false, "geometry": true, "physics": false, "sources": true, "regime": true,
"flow_regime": "hyperconcentrated"
```

| gate | verdict | numbers |
|---|---|---|
| **G1 volume provenance** | **FAIL** | supplied 2.9675e+06 m³ against impounded 2.7000e+07 m³ — **ratio 0.110**, tolerance 0.95–1.05 |
| G2 manufactured mass | pass | clipped 0.0 m³, fraction 0.0 |
| G3 reachable outlet | pass | escape head 55.53 m vs 58.0 m threshold |
| **G4 impoundment retention** | **FAIL** | spill 3768.10 m vs water level 3796.58 m — **28.47 m over** |
| mass closure | (tautology) | relative error **1.10e-15** |

```
initial pool            2.9675e+06 m³   (9 wet cells at 111.6 m)
volume_injected_m3      0.0             <- C1 gone on the central arm
volume_clipped_m3       0.0
outflow_gross_m3        0.0             <- nothing left the domain in 3600 s
barrier emplaced        crest 3794.11 m [RECONSTRUCTION], 2 of 4 barrier cells
                        raised, max raise 49.79 m, 8.237e+05 m³ of structure
breach opening          over 1 cell, crest 3794.11 -> invert 3744.32 m,
                        B = 28.8 m over t_f = 1808 s
control volume          59 cells
```

**Is Q genuinely emergent? Yes — confirmed independently.** The log states "The
injection is deleted; Q is now measured across 59 control-volume cells", and
`volume_injected_m3 = 0.0` with `mass in = 2.968e+06` exactly equal to the initial
pool. `run_2d_swe_simulation` empties `_boundaries` when `breach_opening` is set
(`swe_2d.py:851`) and raises if both are supplied (`:844–850`). **`_boundaries` is
empty. CONFIRMED.**

**NEW CONFIRMED FACT (N-14) — G1 now fails for the opposite reason it was built
for, and no earlier Part reports this.** §48 states C1 is "fixed on the central
arm, by construction … `supplied_m3 = 2.9675e6`, identical to
`initial_pool_volume_m3`". Both halves are true. What §48 omits is that G1 does
not compare `supplied` to `initial_pool_volume_m3` — it compares it to
`impounded_vol_m3`, the volume the **scenario** claims the reservoir held. That is
2.70e+07 m³. So the domain is carrying **11 % of the impoundment it claims to be
draining**, and G1 correctly fails. The double count is gone; an **under-fill of
9.1×** has taken its place.

Its immediate cause is visible in the log: `initial_depth wired from
ASSUMED_FRACTION_LAKE (wet cells=9, volume=2.968e+06 m³)`. At coarsen 4 the lake
resolves onto **nine 111.6 m cells**. The DEM fill cannot hold 2.7e7 m³ in nine
cells, and the configured volume is never reconciled against what the fill
produced.

**NEW CONFIRMED FACT — the `wse_m` argument does not survive to the gate.**
`execute_full_simulation(wse_m=3794.11)` was called; the validity block reports
`initial_wse_m: 3796.58` and G4 evaluates against 3796.58. The caller's water
level is overwritten between the signature and `run_pipeline.py:1858`. This is
consistent with FS-32 (`wse_m` "entirely dead — overwritten at
`run_pipeline.py:444/447` before first use") and extends it: **the overwritten
value is what the retention gate judges**, so the number a user supplies and the
number they are failed against are different.

**CONTRADICTION — `memory.md`'s G4 figures.** `memory.md` §"The pools are filled
above what their own basins hold" records `phutkal escapes its basin at 3779.07 m,
configured 3794.11 m → 15.04 m over`. This run measures **3768.10 m spill against
3796.58 m → 28.47 m over** at coarsen 4. Both halves differ. The memory entry does
not state its resolution, and the figure is resolution-dependent — `spill_level_m`
is computed with `interface_bed=True`, which is exactly the face averaging RC-2
describes, and that gets worse as cells coarsen. The entry needs its coarsening
recorded or it will keep being read as a constant.

### 56.4 C4 — grid convergence

**NOT RUN — deliberately deferred, and this is the honest reason.** A depth- or
arrival-field convergence study is interpretable only once the field being refined
is the field the scheme is supposed to produce. §55.4 measures the bed
reconstruction the solver integrates against as carrying a 2.09 m per-axis mean
error at 28 m and 10.40 m at 112 m; refining the grid changes the *terrain the
solver sees*, not only the discretisation of a fixed terrain. A convergence table
taken now would measure the convergence of that error, which §55.4 already
measures directly and more cleanly (it converges, roughly first order, and does
not reach zero within the data the project has). §50's F-5 already schedules this
for *after* E-1/E-2, which is the only point at which it answers a question about
the flood rather than about the bed. Running it now would produce a number Part
VII would have to discard. Listed in §60 as UNKNOWN with the command that settles
it.

---

## 57. C — New confirmed defects

Each carries file:line, mechanism, and the evidence that established it.

### N-1 — The measured breach discharge reaches no artifact, no manifest, and no UI

**`run_pipeline.py:854–863`** writes `hydrograph.json` containing **only** the
three 0-D routed arms (`t_s`, `Q_m3s`, `Q_p`). The live phutkal run's
`hydrograph.json` has keys `['pessimistic','central','optimistic']` and each arm
has exactly `['t_s','Q_m3s','Q_p']`. The pipeline result dict has no breach-Q key
at all (verified: 39 keys, none matching `breach_q*`). `src/api/main.py:295`
`get_hydrograph` serves that file.

`SimulationResult.breach_q_m3s`, `breach_q_t_s`, `breach_invert_m` and
`breach_width_m` (`swe_2d.py:280–283`, populated at `:1081–1083`) are therefore
**computed and discarded**.

**This is a §1(a) violation of the headline result of Part III.** Q was made an
output in the solver; the number the product reports as "the breach hydrograph"
is still the prescribed 0-D weir curve. Nothing downstream — not the manifest,
not the API, not the frontend chart — can see the measured value. It is also why
§55.8's ratio cannot be reproduced from the repository: the two curves are never
exported together.

### N-2 — An uncited 1.15× multiplier on the dam height, in the production breach path

**`run_pipeline.py:842`**

```python
dam = DamGeometry(
    height_m=dam_height, volume_m3=impounded_vol_m3,
    dam_height_m=dam_height * 1.15, failure_mechanism=mechanism,
```

No comment, no citation, no provenance label. It feeds `macdonald.compute` via
`Hd = max(dam.dam_height_m, Hw)` (`src/m3_breach/macdonald.py:74`), which sets the
breach prism thickness `w_mean = 10.0 + 2.25·Hd` and hence
`B_avg = V_eroded/(Hd·w_mean)`. A 1.15× height therefore changes the breach width
the ensemble reports.

§41 and §51 ban exactly this shape. Part V confirmed the removal of
`q_p_env * 1.15` in `cascade.py:469` — that removal is genuine and the comment in
its place documents it — but an **unrelated and undocumented 1.15 survives in the
orchestrator**, which no previous Part searched.

### N-3 — A fabricated ±15 % "sensitivity band" is exported as a result

**`src/m7_ranking/ranker.py:157–158`**

```python
merged["score_sensitivity_hi"] = (merged["priority_score"] * 1.15).clip(upper=1.0).round(4)
merged["score_sensitivity_lo"] = (merged["priority_score"] * 0.85).clip(lower=0.0).round(4)
```

The code comment is honest ("Fixed ±15% sensitivity band — NOT a per-arm ensemble
spread … Do not read this as uncertainty from the ensemble"), but both columns are
in `output_cols` and ship in `results.geojson`, where a consumer reads them as an
uncertainty range. **§1(b): the numbers come from no computation.** The disclosure
lives in a comment no user sees.

### N-4 — `q_p_env` is a dead configuration parameter

**`src/m3_breach/cascade.py:413`** reads `q_p_env = float(ens_cfg["peak_q_m3s"])`.
Its only other occurrence in the file is inside the comment at line 469 explaining
that the clamp which used it was removed. The variable is assigned and never used,
so `ens_cfg["peak_q_m3s"]` is a scenario-config value with **no causal path to any
solver or output** — §1(a). (Sweep B3.)

### N-5 — The GPU backend still carries the P4 arrival-time defect

**`src/m4_solvers/swe_2d_gpu.py:291`**

```python
arrival_time_gpu = cp.where(h >= arrival_depth_m, 0.0, cp.nan)
```

against the CPU path at **`src/m4_solvers/swe_2d.py:803`**

```python
# P4 (audit §39): a cell that is under the reservoir at t = 0 has not been
# "reached by the flood" at t = 0 … They keep NaN until the flood actually
# adds depth over them.
arrival_time = np.full(h.shape, np.nan)
```

The GPU mirror seeds `0.0` for every initially-wet cell — precisely the defect P4
fixed, which put the whole impoundment into the arrival raster as instantly
inundated and made two villages report a max depth of exactly the reservoir depth.
`backend="gpu"` is a public parameter of `run_2d_swe_simulation` and §56.1 shows
cupy is live on this machine. **CONTRADICTION** with the record that P4 is fixed:
it is fixed on one backend of two.

### N-6 — `tests/test_mass_gates.py` tests a copy of the gates, not the gates

**`tests/test_mass_gates.py:29–42`** defines `G1_TOLERANCE`, `G2_TOLERANCE`,
`_gate_g1()` and `_gate_g2()` locally, under the comment *"Must match
run_pipeline.execute_full_simulation."* Every G1 and G2 assertion in the file
exercises those local copies; nothing imports from `run_pipeline`.

Change the tolerance, the numerator, or the sign of the real gate at
`run_pipeline.py:1817–1849` and **all five tests still pass**. The gates
themselves are sound — §56.3 shows G1 and G4 failing on live data and §58's B5
construction shows G2 failing — but the suite does not pin them. This is the C5
shape one level up: the gate can fail; its test cannot notice if it stops being
able to.

### N-7 — `test_gpu_lake_at_rest_stays_at_rest` cannot fail, for the same reason as its CPU twin

**`tests/test_swe_gpu.py:100–103`** builds `z = 100 + 40x + 15·sin(3y)`, dx = 30 m,
level 130 m, open boundaries — **the identical terrain**
`tests/test_lake_at_rest_bounded.py::test_the_shipped_benchmark_lake_leaves_the_domain_almost_entirely`
proves retains **0.002 %** of its lake in 300 s. Its wetness mask is
`h_final > 0.01`, looser still than the 1.0 m the bounded file uses, so it samples
even more nearly-dry cells. Its docstring names it "Direct GPU analogue of the CPU
test_lake_at_rest_is_still" — it is, including the defect. §50's F-1 must replace
**both**.

### N-8 — The solver returns NaN state without raising

`scratchpad/b5_g3.py`: with `cfl = 2.5` on a 60 m bed step,
`run_2d_swe_simulation` **completes normally** and returns

```
volume_clipped_m3                  = nan
mass_closure()['relative_error']   = nan
np.isnan(max_depth_grid).any()     = True
```

No exception, no warning, no NaN check anywhere in `run_2d_swe_simulation`. The
validity gates fail closed **by accident** — `nan <= tol` and `abs(nan) < 0.01`
are both `False`, so `g2_ok` and `mass_closure_ok` come out `False` — but the
reported reason is *"G2 manufactured mass: clipping created nan m^3"*, which names
the wrong defect. A diverged solve is reported as a clipping-tolerance breach, and
`max_depth.tif` would be written with NaN. Production runs at `cfl = 0.35`, so
this does not bite today.

### N-9 — G3 passes vacuously when the release cell sits on the nodata wall

`escape_head_4connected` returns the release cell's own elevation when that cell is
already the domain maximum, giving head = 0.00 m, which is `< dam_height` for any
dam. Demonstrated on phutkal coarsen 4 at the conditioned wall cell:

```
wall value 6503.6 m at (0,0); escape level 6503.59 -> head 0.00 m -> G3 PASSES
```

`run_pipeline.py:1857–1866` has no guard distinguishing "water can trivially
leave" from "the release cell is inside the 6504 m nodata wall, where the question
is meaningless". `validate_geometry` requires the breach point on the mapped
river, which makes this hard to reach in production, but the gate itself cannot
tell the two apart.

### N-10 — `compare_arrivals` compares model time against observations on a different clock

**`src/m10_validation/compare_arrivals.py:38`**

```python
"obs_arrival_min_range": [30.0, 40.0],  # T+30 to T+40 min relative to 05:45 IST overtopping
```

Every entry in `HISTORICAL_ARRIVALS` is anchored to **05:45 IST overtopping**.
`src/scenarios.py::load_event_clock` and
`tests/test_event_clock.py::test_annamayya_origin_is_the_washout_event_not_overtopping`
pin T = 0 as the **06:30 washout** — that test exists specifically to assert the
origin is *not* the overtopping initiation.

`compare_arrivals:238` sets `modeled_arr_min = t_s / 60.0` — simulation time from
T = 0 — and `:246–257` classifies MATCH / EARLY / LATE against the 05:45-anchored
window. **The two clocks are 45 minutes apart.** It runs in production at
`run_pipeline.py:1678`. It is latent only because annamayya has no DEM (§56.2);
the moment that tile returns, the comparison emits skill verdicts wrong by 45
minutes, labelled `evidence_class: OBSERVED`. **CONTRADICTION.**

The same split shows in the UI: `frontend/spine.js:198` anchors the washout at
**06:15 AM IST** while `:209` labels the MHA failure time **06:30 AM IST**. One
panel, two timestamps, one event.

### N-11 — `src/m4_solvers/pysph_runner.py` is dead

145 lines. Referenced by nothing in `src/`, `run_pipeline.py`, `scripts/` or
`tests/` (whole-tree symbol scan). Not even a test imports it. (Sweep E1.)

### N-12 — `src/gee_satellite.py` is reachable only from its own test

161 lines. Zero production references. `src/api/main.py:634` handles
`kind == "sar"` but routes to `validated_observation`, not to this module.
`frontend/map.js:974–1000` still creates the `gee-sar` source and layers and
`:1691` still fetches `/api/layers/{key}/sar`, while `data/satellite/` is now
**empty**. The machinery is intact, the data is gone, and the only caller is
`tests/test_lake_cascade_gee.py`. (Sweep E1.)

### N-13 — Test A3's velocity bound is wrong, and passes only because friction is on

Full evidence in §55.7. `tests/test_breach_opening.py:206` uses the pool depth
(55 m) where the physical fall is 95 m, making the bound **1.31× too tight**; the
test survives only because the fixture carries `manning_n = 0.03`. At
`manning_n = 0` the same geometry measures **34.73 m/s and the assertion fails**,
while remaining comfortably below both the correct free-fall bound (43.17 m/s) and
the Ritter dry-bed front speed (46.46 m/s).

### N-14 — The impoundment is under-filled 9.1× at production resolution

Full evidence in §56.3. Live phutkal coarsen 4 supplies **2.9675e+06 m³** against
a configured impoundment of **2.7000e+07 m³**, G1 ratio **0.110**, because the
lake resolves onto **nine** 111.6 m cells and the DEM fill is never reconciled
against the configured volume.

---

## 58. D — Conformance findings by sweep (§1(a)/(b)/(c))

### Sweep B1 — hardcoded values presented as computed results

**The single worst finding in this Part.**
`data/validation/malpasset/malpasset_benchmark.json` (2 239 bytes, on disk, dated
2026-09-04) contains, alongside every `measured_*` field, a matching fabricated
`simulated_*`:

```json
{ "station": "Transformer A", "distance_km": 1.4, "measured_s": 100,  "simulated_s": 105 },
{ "station": "Transformer B", "distance_km": 5.5, "measured_s": 1204, "simulated_s": 1180 },
{ "station": "Transformer C", "distance_km": 6.9, "measured_s": 1420, "simulated_s": 1395 },
{ "point": "P1", "distance_m": 422,  "measured_wse_m": 80.3, "simulated_wse_m": 81.2 },
{ "point": "P2", "distance_m": 1000, "measured_wse_m": 71.8, "simulated_wse_m": 70.9 }
```

and describes itself as *"Validated against actual recorded transformer
destruction times and police surveyed high-water marks."*

**Nothing in this repository produced any `simulated_*` value.** §56.2 proves
malpasset fails `validate_geometry` at every coarsening
(`upstream/downstream seeds lack separated connected components`), so it has never
run and cannot run. The file is served live at `src/api/main.py:673`
`get_malpasset_benchmark`, fetched by `frontend/charts.js:375`, and **plotted**
into `#malpasset-hwm-chart`. A user sees a simulated-vs-measured agreement chart
for a simulation that does not exist. This is FS-46, still open, and it is a
§1(b) violation at the point of display rather than merely on disk.

`src/m10_validation/compare_arrivals.py:30–100` — `HISTORICAL_ARRIVALS` is a
hardcoded table, but every entry carries an `id` (EVD-21…EVD-26), an
`evidence_class`, and a named `source`. As *observations* that is defensible
provenance; the defect is the clock offset (N-10), not the literal values.

`run_pipeline.py:824–825` — on DEM-fill failure the pipeline falls back to
`volume_mcm = sc_cfg.get("volume_mcm", 28.5)` and
`dam_height = sc_cfg.get("dam_height_m", 55.0)`. A default dam height of 55.0 m
for an unknown structure is a fabricated dam, mitigated by the
`Provenance.PROXY_DATA` label and a warning log. Disclosed — but it is a default
that invents geometry, the same family §41 bans.

**Nothing else found.** No `simulated_*` field, no RMSE/CSI/POD figure without a
run behind it, and no invented shelter or facility in the Python backend; see
Sweep D1 for `frontend/map.js`.

### Sweep B2 — multipliers, caps, clamps, envelope limits

`cascade.py:469`'s `min(..., q_p_env * 1.15)` is **gone**, and the comment in its
place documents the removal correctly. **CONFIRMED EXISTING KNOWLEDGE.**

Two survivors, reported above: **N-2** (`run_pipeline.py:842`,
`dam_height * 1.15`, uncited, in the physics path) and **N-3**
(`ranker.py:157–158`, ±15 % band, disclosed only in a comment).

Everything else examined in `swe_2d.py` and `m3_breach/` is a legitimate numerical
guard, recorded here so the next sweep does not re-flag it: `swe_2d.py:939`
`max_wave = max(max_wave, 0.5)` and `:940` `dt = min(..., 5.0, ...)` only ever
**reduce** `dt`; `:1011` / `:1022` `np.minimum(h, 0.0)` is the clipping counter G2
gates on; `breach_kernel.py:39/50` `min(1.0, elapsed/t_f)` is the growth law
saturating at its final width.

One is worth a note: **`swe_2d.py:941` `dt = max(dt, 1e-3)` is a floor, not a
cap** — it can violate CFL rather than enforce it, silently and with no
diagnostic. At production cell sizes the required `dt` is ~1 s, three orders above
the floor, so it does not bite; it is the only guard in the file that can act
against stability rather than for it.

### Sweep B3 — dead parameters

**N-4** (`q_p_env`) is the only fully dead parameter found on the physics path.
Adjacent, reported elsewhere: `wse_m` reaches the gate as a different value than
the caller supplied (§56.3), and the whole `breach_q_*` family is computed and
discarded (N-1).

### Sweep B4 — defaults that fabricate structure

**No surviving `wse_m + 5.0`, under that name or any other.** A tree-wide search
for a crest defaulted rather than sourced returns nothing in `src/` or
`run_pipeline.py`. `validate_geometry` refuses: §56.2 shows derna and south_lhonak
failing with *"geometry manifest requires a finite sourced crest_elev_m — the
barrier cannot be emplaced at an assumed elevation"*. The invariant holds and is
enforced by a gate that demonstrably fires. **CONFIRMED EXISTING KNOWLEDGE —
closed.**

The one default that still invents geometry is the `dam_height_m 55.0` /
`volume_mcm 28.5` proxy fallback noted under B1.

### Sweep B5 — gates that cannot fail

Every gate was given an input that should fail it.

| gate | failing input | result |
|---|---|---|
| geometry | derna, malpasset, ivanovo, south_lhonak manifests on their real DEMs | **FAILS**, four distinct messages (§56.2) |
| G1 | live phutkal c=4: pool 2.97e6 against configured 2.70e7 | **SUPERSEDED 2026-09-18** — this figure does not reproduce at any resolution, level or barrier state tried. G1 now **passes** at ratio 1.0000034 after the `reservoir_fill` stage-vs-volume defect was fixed |
| G2 | `cfl = 0.9` on a 60 m bed step | **FAILS**: clipped 9.2337e+05 m³ = **2.012 %** of input, tolerance 0.1 % |
| G3 | — | **not constructed**; passes at 55.53 / 58.0 on live phutkal, and **N-9** shows a vacuous-pass path |
| G4 | live phutkal c=4 | **FAILS**, 28.47 m over (§56.3) |

Four of five are proven able to fail on inputs that reach them. G3 is the
exception: it passed in every configuration tried, by a 2.47 m margin on real
data, and N-9 documents a class of input it cannot judge at all. **No gate was
found to be a tautology.** The tautology has moved into the *tests* of the gates
(N-6).

### Sweep B6 — labels that are not physics

`src/m3_breach/__init__.py:40–62` declares **thirteen** `FailureMechanism`
members; `_IMPLEMENTED_MECHANISMS` contains **two** (`OVERTOPPING_EROSION`,
`PROGRESSIVE_BREACH`) and `require_implemented_mechanism` raises for the other
eleven. `tests/test_failure_mechanism.py:37`
`test_overtopping_and_progressive_breach_share_identical_kernel` **asserts the two
survivors are identical**.

So: eleven mechanisms refuse, two compute the same thing, and the mechanism
parameter selects nothing. Part I's M3 finding stands unchanged — with the
difference that it is now *disclosed and pinned by a test* rather than hidden.
**CONFIRMED EXISTING KNOWLEDGE**, correctly handled. The honest position is that
FloodSight models one failure mechanism, not thirteen, and the enum must not be
read as a capability list.

One gap: **`src/m3_breach/macdonald.py::compute` does not call
`require_implemented_mechanism`**, while `froehlich.py:40` and `von_thun.py:55`
both do. The production path is gated upstream at `run_pipeline.py:839`, so this
is a robustness inconsistency rather than a live hole.

### Sweep B7 — citation defects

FS-39's two substitutions are **still present in behaviour and now disclosed in
comments**:

- **`src/m3_breach/von_thun.py:78`** — `Q_p = 0.607·Vw^0.295·Hw^1.24`, Froehlich
  (2008) verbatim, identical to `froehlich.py:50`. Evidence from the live run: the
  arm labelled `pessimistic` and the arm labelled `optimistic` both report
  `Q_p = 14524.183042643774` — **bit-identical**, because both are the same
  Froehlich regression on the same `(Hw, Vw)`.
- **`src/m3_breach/macdonald.py:86`** — `t_f = 63.2·√(Vw/(g·Hw²))`, Froehlich's
  formation time, under the comment "Froehlich t_f as stand-in".

Both now carry explanatory comments, which Part IV did not record. **Status: open,
but downgraded from silent to disclosed.** The disclosure does not reach the
artifact: `hydrograph.json` reports `Q_p` under `"method": "VonThunGillette1990"`
with nothing marking it as Froehlich's number.

Three further citation defects recorded in `memory.md` §"Three citation defects in
m3_breach, verified against USACE" were checked and are **all still present**:

- `froehlich.py:44` uses `k_o = 1.4`. Froehlich (2008) specifies **1.3**.
- `macdonald.py` is titled, `method=`-stringed and referenced as **"MacDonald &
  Langemeier (1984)"**. The author is **Langridge-Monopolis**. Its docstring at
  `:24` asserts *"t_f: not given by this method"*; M-LM define
  `t_f = 0.0179·V_eroded^0.364` hours.
- `von_thun.py:16` documents a piping branch `t_f = (B/4)/(1.4·H_w)` that appears
  in no source, and `:18–20` documents `Q_p ≈ (1/3)(B_avg + Z·H_w)√(2g)·H_w^1.5`
  — **an equation the code at `:78` does not compute**. The module's docstring and
  its implementation disagree.

**§1(c): a formula attributed to an author who did not publish it, and a docstring
documenting an equation the function does not evaluate.**

### Sweep D1 — frontend and README claims vs the backend

| ID | status | evidence |
|---|---|---|
| **FS-15** | **CLOSED** | `frontend/index.html:602–608` now reads *"Delft3D has never been run in this project"*, `Result shown: none`, and records that the previously displayed "Ritter RMSE 0.14 m" was removed rather than relabelled. **Part IV §44.4 is STALE on this.** |
| **FS-18** | **PARTIAL** | the visible toggle is gone and `index.html:47–50` documents why; `map.js:974–1000` still builds the `gee-sar` source + two layers, `:1691` still fetches `/api/layers/{key}/sar`, and `data/satellite/` is empty |
| **FS-19** | **OPEN, verbatim** | `frontend/spine.js:198` still paints *"Full washout of 336m earthen bund at 06:15 AM IST (T+30 min), peak release ~12,200 m³/s (EVD-17/18)"* |
| **FS-22** | **OPEN** | 8 hardcoded `wse:` values remain in `map.js`; `:58` is `wse: 2450` for rishiganga |
| **FS-26** | **OPEN** | `map.js:49–50, 442, 640–661` — `shelterMarkers`, `sheltersVisible`, `_addSheltersLayer`, a `shelters` source and two rendered layers |
| **FS-46** | **OPEN, worse than recorded** | not merely served — **plotted**. `charts.js:371–375` renders it into `#malpasset-hwm-chart` |
| **FS-16** | **STATUS CHANGED** | no longer an unreachable stub. `run_pipeline.py:1316–1340` runs `run_scenario_thalweg_sph`, and the live run's `solver_comparison.json` returns `available:false, status:NOT_AVAILABLE` with a **stated reason** and a `what_would_make_it_real` field. Honest refusal, not dead code. Part IV is stale. |
| **FS-41** | **CLOSED** | `src/api/main.py:78–84` — `_detect_scenario_key_and_name` deleted, with the reason recorded in place |
| **FS-08** | **PARTIAL** | **two** `scenario_key == "annamayya"` branches remain (`run_pipeline.py:722, 769`), down from three |
| **FS-42** | **OPEN** | `../check_all_rasters.py`, `../check_breach_location.py`, `../check_depth_data.py` still duplicated at the repository parent |

**New D1 finding:** FS-19's spine text is not merely unsupported, it is
*counter-supported* — see N-10's closing paragraph.

### Sweep D2 — ATLAS

`python scripts/gen_atlas.py` → *"wrote ATLAS.md (541 lines)"*, and
`git diff --stat ATLAS.md` is **empty**: the atlas regenerates byte-identical, so
it matches the tree. Header reads `generated: 2026-09-13 01:05`, `newest source in
tree: scripts/gen_atlas.py (2026-09-12 20:36)` — generated **postdates** newest
source, so it is not stale. **No finding.**

### Sweep D3 — INVARIANTS

Checked entry by entry against current source. Every routing row in §1 and every
trap in §3 verified correct, with three exceptions — all numbers, not claims:

1. §2 *"a one-cell wall passes 46.7 % … 0.000 m³ passes a two-cell wall"* —
   fixture-specific; this session measures 55.4 % and 7.96e+01 m³ (§55.3).
2. §2 *"At the production coarsen-4 that error averages 20–30 m"* — that is the
   two-axis sum; the per-axis error each flux sees is 10.4–15.7 m (§55.4).
3. §3 *"Measured Q peaking at 2.7× the routed value … Sustained ratio is 0.603"* —
   the measured half reproduces exactly; the ratio does not (§55.8).

One trap needs **adding**, not correcting: `INVARIANTS.md` §3 treats P4 as settled;
**N-5** shows it is settled on the CPU backend only.

### Sweep D4 — tests that pass while proving nothing

Three found. `test_lake_at_rest_is_still` (`tests/test_swe_validation.py:71`) is
**CONFIRMED EXISTING KNOWLEDGE** — already documented in
`tests/test_lake_at_rest_bounded.py` and `INVARIANTS.md` §3. The two new ones are
**N-6** (`test_mass_gates.py` tests local copies of G1/G2) and **N-7**
(`test_gpu_lake_at_rest_stays_at_rest` inherits the drained-lake defect). **N-13**
is a fourth of a different kind: a test that asserts against a bound computed
wrongly.

Also checked and **clean**: no test asserts on a constant it also defines, no test
asserts textual ordering, and no test silently skipped in the §56.1 run.

### Sweep E — dead weight and provenance

- **E1**: **N-11** (`pysph_runner.py`, dead) and **N-12** (`gee_satellite.py`,
  test-only). `src/m3_breach/event_graph.py` is **deleted** — only a stale
  `__pycache__/event_graph.cpython-312.pyc` remains. **FS-20 is CLOSED**; Part IV
  is stale on it.
- **E2**: two independent arrival-time computations, and they **disagree** —
  **N-5**. The third path Part IV counted (`run_pipeline.py`'s raster pass) is a
  *consumer* of `SimulationResult.arrival_time_s_grid`, not an independent
  computation; `m6_isolation/isolation.py:414` samples the same raster.
  **FS-40 revised: two computations, not three, and the disagreement is the
  finding.**
- **E3**: the fabricated artefacts Part IV listed are **largely gone from the
  working tree** — `data/satellite/` is empty (five fake "Sentinel-1 SAR" polygons
  deleted), `data/validation/annamayya/` is empty, and
  `data/validation/gfd_dam/ivanovo_observed.geojson` is deleted.
  `data/validation/gfd_dam/` retains only real DFO GeoTIFFs. **The one that
  remains is the worst one: `malpasset_benchmark.json` (B1 / FS-46).**
- **E4**: **125 of 128** run directories under `data/scenarios/` have no
  `manifest.json`, totalling **442 MB**. Unchanged from Part IV.
  `_rehydrate_saved_scenarios` reads only valid manifests, so they are inert — but
  they are 442 MB of unprovenanced output on disk.
- **E5**: `data/validation/README.md` now says the **opposite** of what Part IV
  recorded. It no longer claims "Nothing here is synthetic"; it states the
  artefacts *"WERE synthetic, were served as ground truth"* and is explicitly
  *"NOT a claim that nothing in this repository is synthetic"*. **STALE
  DOCUMENTATION in Part IV — this one is fixed, and fixed well.**

### Sweep D5 — records contradicted by code

- `memory.md` §"The pools are filled above what their own basins hold": phutkal
  15.04 m over → **measured 28.47 m at coarsen 4** (§56.3). Needs its resolution
  recorded.
- `memory.md` §"Zero of seven scenarios now produce a valid run": annamayya is
  recorded as failing for "no river role"; it now fails earlier, with **no DEM at
  all** (§56.2). south_lhonak is recorded as having "no manifest under its own
  key"; both manifests exist and the gate reaches the **crest-sourcing** refusal.
- `INVARIANTS.md`: three numeric corrections listed under D3.

---

## 59. E — Updated defect register

Only entries whose status **changed** or was **established this session**.
Everything else stands as Parts I–V left it.

### Root causes

| ID | Part V | Part VI | evidence |
|---|---|---|---|
| RC-1 | CONFIRMED | **CONFIRMED, exact** | §55.1, residual 0.000e+00 |
| RC-2 | CONFIRMED | **CONFIRMED, exact** | §55.2, all four rows |
| RC-3 | CONFIRMED | **CONFIRMED; 46.7 % / 0.000 m³ corrected to 55.4 % / 7.96e+01 m³** | §55.3 |
| RC-4 | CONFIRMED | **CONFIRMED, exact; metric = 2-D 5-point lap/4; per-axis error is half the quoted figure** | §55.4 |
| RC-5 | CONFIRMED | **CONFIRMED, unchanged** | §55.5 |
| RC-6 | CONFIRMED, minor | **mechanism CONFIRMED; the 28.4 / −17.5 m/s magnitudes unverified** | §55.6 |

### Part I criticals

| ID | Part V | Part VI |
|---|---|---|
| C1 | fixed on the central arm, open on the envelope arms | **unchanged** — and §56.3 shows the central arm now fails G1 the *other* way, at ratio 0.110 |
| C2 | fixed | **unchanged** — the opening is cut on the structure; `snap_to_thalweg` no longer positions the release |
| C3 | emplaced, defeated by RC-2/RC-3 | **unchanged** — live run: 2 of 4 barrier cells raised, opening **1 cell** wide |
| C5 | fixed (gates can fail) | **gates confirmed able to fail (B5); their tests cannot (N-6)** |
| C9 / FS-46 | open | **open, and confirmed rendered in a chart** |

### FS backlog — changes only

| ID | was | now |
|---|---|---|
| FS-15 | open | **CLOSED** — Delft3D card states no run, shows no result |
| FS-16 | open (unreachable stub) | **CHANGED** — runs, and refuses honestly with a stated reason |
| FS-20 | open | **CLOSED** — `event_graph.py` deleted |
| FS-41 | open | **CLOSED** — `_detect_scenario_key_and_name` deleted |
| FS-17 / 47 / 51 / 54 | open | **MOSTLY CLOSED** — fake SAR + annamayya + ivanovo artefacts deleted; `README.md` rewritten to disclose |
| FS-18 | open | **PARTIAL** — branding gone, machinery and endpoint remain |
| FS-08 | 3 branches | **2 branches** |
| FS-39 | open, silent | **open, disclosed** — plus three further citation defects confirmed still present |
| FS-40 | 3 arrival paths | **2 paths, and they disagree (N-5)** |
| FS-32 | `wse_m` dead | **worse — the overwritten value is what G4 judges (§56.3)** |
| FS-19, FS-22, FS-26, FS-42, FS-03/48, FS-46 | open | **open, verbatim** |

### New — Part VI

| ID | severity | one line |
|---|---|---|
| **N-1** | **critical** | measured Q reaches no artifact, manifest or UI — §1(a) on Part III's headline result |
| **N-2** | **critical** | uncited `dam_height * 1.15` in the production breach path (`run_pipeline.py:842`) |
| **N-13** | **critical** | test A3's free-fall bound is 1.31× too tight and passes only because friction is on |
| **N-14** | **CLOSED 2026-09-18** | the 9.1× under-fill does not reproduce. The real defect was `reservoir_fill` read as a STAGE fraction for the initial condition and a VOLUME fraction for `impounded_vol_m3` — a 0.894 ratio, not 0.110. Fixed; G1 passes |
| N-3 | major | fabricated ±15 % sensitivity band exported as a result |
| N-5 | major | GPU backend still carries the P4 arrival-time defect |
| N-6 | major | `test_mass_gates.py` tests copies of the gates, not the gates |
| N-7 | major | `test_gpu_lake_at_rest_stays_at_rest` cannot fail |
| N-10 | major | `compare_arrivals` uses a T = 0 forty-five minutes from the event clock |
| N-4 | minor | `q_p_env` dead config parameter |
| N-8 | minor | solver returns NaN state without raising |
| N-9 | minor | G3 passes vacuously on a nodata-wall release cell |
| N-11 | minor | `pysph_runner.py` dead |
| N-12 | minor | `gee_satellite.py` test-only, its data deleted |

---

## 60. F — Fixed, open, and unknown

### Genuinely fixed, verified this session

- The injection is gone from the central arm and **cannot be re-added quietly** —
  `run_2d_swe_simulation` raises when both are supplied, `_boundaries` is empty
  when an opening is present, `volume_injected_m3 = 0.0` on a live run.
- **Q is genuinely emergent** on the central arm, and its integral closes to 1.2 %.
- **The crest is sourced or the run is refused.** Two scenarios fail the geometry
  gate on exactly that, with a message that names it.
- **The gates can fail.** Four of five proven on real or constructed input; two
  fail on the live phutkal run.
- The near-breach jet is **physical** — confirmed by a sweep spanning a 16× change
  in the numerical term with no change in velocity.
- The fabricated SAR / annamayya / ivanovo artefacts are **off disk**, and
  `data/validation/README.md` now discloses rather than denies.
- FS-15, FS-20, FS-41 closed.

### Still open, in the order that matters

1. **RC-1 + RC-2** — the flux operator. Everything below depends on it.
2. **N-1** — the measured discharge is thrown away.
3. **N-2** — an uncited multiplier in the physics path.
4. **N-13** — the velocity bound test is wrong in two directions.
5. **N-14 / G1** — the impoundment is under-filled 9×.
6. **G4** — every scenario is configured above what its basin holds.
7. **RC-5 / E-4** — the envelope arms still double count.
8. **FS-46** — a fabricated benchmark is plotted in the UI.
9. **N-5, N-6, N-7** — the instruments are blunt where it matters most.
10. RC-6, N-3, N-4, N-8 through N-12, and the rest of the FS backlog.

### UNKNOWN, and the measurement that settles each

| unknown | what settles it |
|---|---|
| Does the depth/arrival field converge under refinement? | **C4, deferred.** Run the F-5 convergence table **after** E-1/E-2 lands. Running it before measures the convergence of the bed error, which §55.4 already measures directly. |
| Is the sustained measured/routed ratio 0.603 or 0.830? | Export both curves from committed code (fix N-1), then re-measure. Until then neither number is a reference. |
| What are RC-6's true magnitudes? | Part V's fixture is unrecorded. Re-derive from a committed fixture, or drop the 28.4 / −17.5 figures. |
| Does E-1+E-2 remove the RC-4 error, or only shrink it? | F-5: re-run §55.4's table. The per-axis mean must be **0.00** at every coarsening, not merely smaller. |
| Can any scenario be made G4-clean without fabricating a water level? | Needs a sourced WSE per scenario, or a finer grid. An open data question, not a code question. |
| Is the 55.4 % / 7.96e+01 m³ barrier leakage fixture-independent? | F-2 at 1, 2, 3 and 5 cells on **two** fixtures, not one. |

### One-line state of the model after Part VI

> Every root cause Part V named reproduces, most of them to the digit — and the
> two numbers it put in its headline, a 20–30 m free-surface error and a 0.603
> discharge ratio, are each an artefact of how they were aggregated rather than
> of what was measured. Underneath that, the release the pipeline actually runs
> carries 11 % of the water it claims, reports a discharge it computed and threw
> away, and passes its velocity test only because friction is switched on.

---
---

# Part VII — Implementation outcome

**Date:** 2026-09-13. **Status: CODE CHANGED.** This Part records what was
implemented against `implementation_plan.md` and what the register looks like
afterwards. Full measurements and both wrong turns are in
`findings_results.md`, 2026-09-13.

Suite: **211 passed, 0 failed, 0 xfailed** (was 185 passed / 2 xfailed). Both
xfails were cleared by the fix rather than removed; 26 tests were added.

---

## 61. What was fixed

### The flux operator — RC-1, RC-2, RC-3, RC-4 all closed

`swe_2d._rhs` and its GPU mirror now use Audusse et al. (2004) hydrostatic
reconstruction: interface bed `max(z_L, z_R)`, free surface reconstructed against
the cell's **own** `z`, a face-pair bed-slope source, and the
cell-mean-preserving wet/dry tilt removed.

| | before | after |
|---|---|---|
| `_rhs` max \|dh/dt\|, lake at rest, worst topography | 5.043e+01 | **0.000e+00** |
| `_rhs` max \|dhu/dt\| | 3.728e+02 | **1.137e-13** |
| bounded lake, free-surface spread | 69.9–95.9 m | **2.8e-14 m** |
| bounded lake, \|v\|max | 39.9–41.9 m/s | **1.8e-14 m/s** |
| one-cell wall, volume passed in 300 s | 55.4 % of the pool | **0.000 m³** |
| un-breached wall leakage, 300 s | 6.9e+04 m³ (0.23 %) | **0.000000e+00 m³** |
| Ritter RMSE | 0.043 m | **0.042563 m** |

RC-4 is closed **by construction rather than by measurement**: there is no
`z_bar` any more, so `|z_bar − z|` has no referent. The identity is asserted on
`_rhs` itself at coarsen 1/2/4 on real conditioned terrain.

RC-6 is closed as a side effect: with the cell's own bed, edge-padded ghosts
replicate `eta` exactly, so the rim carries no spurious flux.

### Conformance violations closed

- **N-1** — the measured breach hydrograph is exported to `hydrograph.json`
  under `"measured"`, beside the routed arms. It was computed every step and
  discarded.
- **N-2** — the uncited `dam_height * 1.15` is deleted.
- **N-5** — the GPU arrival-time seed matches the CPU's P4 fix.
- **N-8** — a non-finite depth now raises instead of returning NaN quietly.
- **N-11** — `pysph_runner.py` deleted.
- **N-4** — the dead `q_p_env` binding removed.
- **FS-46** — every `simulated_*` key is stripped from
  `malpasset_benchmark.json`; `charts.js` plots the surveyed marks alone and
  labels the chart "no simulation on record".
- **RC-5 / E-4** — all three envelope arms now get their own `BreachOpening`
  built from that arm's `BreachParams`; their injection hydrographs are zeroed.
  `envelope.tif` and `envelope.geojson` no longer carry the 2.00× C1 double
  count.

### Instruments repaired — N-6, N-7, N-13

- `run_pipeline.gate_g1` / `gate_g2` are module-level and **imported** by
  `tests/test_mass_gates.py`. **Proved**: setting the real `G1_TOLERANCE` to
  10.0 previously left all 12 tests green; it now fails 3, including the
  certified-double-count acceptance.
- Test A3's bound uses the real 95 m fall, not the 55 m pool depth, and runs
  with friction **off**. It passes at 34.73 m/s against 43.17 m/s.
- `test_momentum_changes_near_field_velocity` injected uphill under a comment
  calling it downstream. Corrected, and strengthened to pin both directions and
  the sign of total x-momentum.

### Citation defects — B7 partly closed

`froehlich.py` `k_o` 1.4 → **1.3** (B 106.19 → 98.60 m). `macdonald.py` now uses
M-LM's own `t_f = 0.0179·V_eroded^0.364` h instead of Froehlich's (0.502 →
1.779 h), is renamed **MacDonald & Langridge-Monopolis**, and calls
`require_implemented_mechanism`. `von_thun.py`'s invented piping branch and its
wrong `Q_p` docstring are corrected.

**Still open, now disclosed:** von Thun returns Froehlich's `Q_p` verbatim, so
the ensemble carries **two** independent peak-discharge estimates, not three.

---

## 62. New defects found while implementing

### N-15 — a sub-grid breach opens nothing, silently

`run_2d_swe_simulation` opens cells as `mask & (axis_distance <= width/2)`.
phutkal at coarsen 4: a **37.7 m** breach against **111.6 m** cells opened
**0 cells** and measured a peak of **0 m³/s**, while `breach_opening.active`
reported `true` and G2/G3 passed. rishiganga at coarsen 4: B = 20.0 m against
113.4 m cells. **Now refused at construction.**

### N-16 — a corner-connected breach opens nothing, silently

phutkal at coarsen 1: the same breach opened 2 cells, both with **zero
face-adjacent wet neighbours** — nearest wet pool cell 1.4 cells away, i.e.
diagonal. Water crosses faces, never corners. **Now refused at construction.**

### N-17 — the crest gate checked the wrong crest

`crest_ok` compared `wse_m` against `dem_crest_along_axis` — the highest DEM
cell the dam axis crosses, which in a gorge is the **valley wall**. phutkal's
configured `wse_m = 3878.0` sits **84 m above its sourced barrier crest of
3794.11 m** and passed, because the DEM ridge is 3989.38 m. The gate now checks
both crests.

### G5 — a new gate: the release must actually have occurred

Every gate that existed could pass on a run in which no water ever moved. G1
asks where the water came from, G2 that none was invented, G3 that an exit
exists, G4 that the pool is held. **None asks whether the dam broke.** G5 fails a
run whose opening is active and whose measured peak discharge is zero.

---

## 63. phutkal — root cause of both remaining gate failures

One number. `SCENARIOS["phutkal"]["wse_m"] = 3878.0`, against a sourced crest of
3794.11 m.

1. At 3878.0 m `build_stage_storage` floods the valley to the domain edge and is
   correctly rejected.
2. The pipeline falls back to the **typed** `volume_mcm = 30.0`, labelled
   PROXY DATA → `impounded_vol_m3 = 2.70e7 m³`.
3. `initial_depth` is built separately and sanely (90 % fill, **7.40e6 m³**).
4. G1 compares those two: ratio **0.274**. Fails.

**Measured stage-storage** (coarsen 2, barrier emplaced at 3794.11 m):

| level | volume |
|---|---|
| 3772.17 m (basin rim, G4 spill) | 6.77 MCM |
| 3790.00 m | 16.22 MCM |
| **3794.11 m (barrier crest)** | **18.98 MCM** |
| 3800.00 m | 1164.80 MCM ← spills the crest, floods the valley |

**27.0 MCM is not impoundable behind this dam at any water level.** The ceiling
is 18.98 MCM, so G1 cannot pass while `volume_mcm = 30.0` stands. This is
scenario data, not solver error, and per the decision taken this session it is
refused rather than reconciled by relaxing a gate.

---

## 64. The release now runs

phutkal, coarsen 2, live end-to-end:

```
gate_g5_release_occurred: ok = true
  opening_active        = true
  measured_peak_q_m3s   = 1873.81
  cells_opening_at_full_width = {central: 2, pessimistic: 2, optimistic: 0}
gate_g2 manufactured mass : pass (clipped 0.0)
gate_g3 reachable outlet  : pass (escape head 27.02 m vs 58.0 m)
gate_g1 volume provenance : FAIL (7.40e6 vs 2.70e7, ratio 0.274)
gate_g4 impoundment       : FAIL (pool 23.17 m above the 3772.17 m spill level)
mass closure              : 1.38e-15
```

**The pool drains through an opening cut in the structure and the discharge is a
measurement.** That is the first run in this project's history where Q is an
output and non-zero. What remains failing is the scenario's configured water
level and volume, which §63 traces to a single unsourced number.

Note `optimistic: 0` — that arm's breach is still sub-grid at this resolution, so
it contributes a no-release grid to the envelope and understates it. The
sub-grid refusal currently guards the central arm only.

---

## 65. Register delta

| ID | Part VI | Part VII |
|---|---|---|
| RC-1 | CONFIRMED | **CLOSED** |
| RC-2 | CONFIRMED | **CLOSED** |
| RC-3 | CONFIRMED | **CLOSED** — barrier holds at 1 cell |
| RC-4 | CONFIRMED | **CLOSED by construction** |
| RC-5 | CONFIRMED | **CLOSED** — all three arms carry openings |
| RC-6 | CONFIRMED, minor | **CLOSED** as a side effect |
| N-1, N-2, N-4, N-5, N-6, N-7, N-8, N-11, N-13 | open | **CLOSED** |
| FS-46 | open, plotted | **CLOSED** — `simulated_*` stripped |
| N-3 (±15 % band), N-9 (G3 vacuous pass), N-10 (clock), N-12 (gee_satellite) | open | **still open** |
| N-14 (under-fill) | open | **root cause found** (§63); refused, not reconciled |
| **N-15, N-16, N-17** | — | **new, and closed in the same pass** |
| G5 | — | **new gate** |
| FS-19, FS-22, FS-26, FS-42, FS-03/48, FS-18 | open | **still open** (frontend/dead weight) |
