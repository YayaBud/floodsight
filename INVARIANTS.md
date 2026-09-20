# INVARIANTS — FloodSight

Hand-written companion to `ATLAS.md` (which is generated and holds structure only).
This file holds what the atlas structurally cannot say: **where to make a change**,
**what must not break**, and **what looks like a bug and is not**.

Maintenance: update in place, delete what turns out to be wrong. A stale entry here
is worse than none, because it gets trusted. Every entry carries its *why*.

Related records: `../memory.md` (decisions), `../findings_results.md` (measurements
and dead ends), `docs/FLOODSIGHT_PHYSICAL_MODEL_FORENSIC_AUDIT.md` (the master
defect record; Part V is the current root-cause diagnosis).

---

## 1. Routing — "to change X, edit Y"

The established production path for each kind of change. The **near-miss** column is
the file that looks right and is not — these are where a plausible edit lands and
then moves no output.

| To change… | Edit | Near-miss — do NOT edit |
|---|---|---|
| how the breach grows (width, invert, discharge) | `src/m3_breach/breach_kernel.py` — the single shared kernel | `cascade.py` / `ensemble.py` growth code: they **call** the kernel, they don't define it |
| the 0-D reservoir routing | `src/m3_breach/cascade.py::simulate_reservoir_cascade` | `breach_kernel.py` — it is one instant's discharge, not the routing |
| where the opening is cut, and how it evolves | `src/m4_solvers/swe_2d.py::BreachOpening` + the P3 block in `run_pipeline.py` | `snap_to_thalweg` — it no longer positions the release and must not again (it lands on the pool floor: that was defect C2) |
| how the barrier gets into the solved terrain | the P2 block in `run_pipeline.py` (~339) | `fill.py`'s `np.where(barrier_mask…)` — that is a **private copy** for the fill only; editing it does not reach the solver |
| the flux scheme / well-balancing | `src/m4_solvers/swe_2d.py::_rhs` **and `swe_2d_gpu.py::_rhs_gpu` in the same commit** | `_rusanov` / `_rusanov_jit` — correct on the states they receive. Changing one backend and not the other is silent: the two are only ever compared within a wide tolerance |
| adding or changing a G1/G2 gate | `run_pipeline.py::gate_g1` / `gate_g2` (module level) | re-stating the formula in a test — `tests/test_mass_gates.py` did exactly that and could not detect a broken gate |
| what counts as *flood* for consequences | the P4 block in `run_pipeline.py` (~1274) — `flood_depth_grid` | `max_depth.tif` — deliberately still contains the reservoir, for the map |
| adding or changing a validity gate | the G1–G3 block in `run_pipeline.py` (~1801) | `swe_2d.mass_closure()` — a ledger, not a gate; it cannot fail by construction |
| terrain modification before the solve | between the geometry gate (~309) and `open_river_outlets` (~533), nowhere else | anywhere after the solver call — the terrain is already integrated |
| which scenarios exist and their constants | `src/data_fetcher.py::SCENARIOS` | `frontend/map.js` — it carries duplicated hardcoded values (FS-22, known defect) |
| geometry roles, crest, dam axis | `data/geometry/<scenario>.json` + `src/m2_geometry/validation.py` | — |
| what the solver may represent at all | `src/m3_breach/__init__.py` — `FlowRegime`, `regime_status` | — |
| who is scored as at risk, and where they are | `data/admin/annamayya_villages.geojson` (23 features) — this file **is** the population layer | `results.geojson` — an output; editing it changes the report, not the assessment |
| per-village depth, arrival and PAR on the routed run | `scripts/route_annamayya.py::village_exposure` — polygon mask, cell-centre, same semantics as `m5_exposure` | a centroid sample: one 2.3 ha cell standing in for a whole settlement. That was the bug |
| what a settlement's map badge says | `frontend/village_status.js::villageStatus` | the inline block in `map.js` that used to hold it — it now calls the function, and a copy of the rule in a test is what INVARIANTS already warns against |
| the purple layer on the map | `scripts/observed_wse_extent.py` -> `data/admin/{k}_observed_wse.geojson` | `{k}_corridor.geojson` — that was the purple layer until 2026-09-14 and is now diagnostic-only |
| the reconstructed flood corridor layer (diagnostic, off the map) | `data/admin/{k}_corridor.geojson` + `_LAYER_FILES` in `src/api/main.py` | the `observed` kind — corridor is terrain-derived and must never be served as ground truth |
| the Annamayya forced-hydrograph run | `scripts/route_annamayya.py` | `run_pipeline.py` — annamayya never reaches it, it fails the geometry gate first |
| the elevation below which a DEM is artefact, not ground | `SCENARIOS[k]["dem_floor_m"]`, passed as `condition_dem(floor_m=...)` | `thalweg_m` — it is the DAM-SITE bed, not the domain minimum. See the trap below |
| what arrival validation is allowed to score against | `data/observations/<k>/arrivals.json`, authored by `scripts/author_arrival_manifest.py` | `compare_arrivals.HISTORICAL_ARRIVALS` — a legacy audit trail that **nothing reads**; `_verified_arrivals` reads the manifest file |
| whether a cached OSM layer matches the scenario AOI | the `<name>.bbox.json` sidecar written by `_bbox_stamp_ok` | the layer's mtime — it says when it was fetched, never which bbox for |

## 2. Invariants — properties other code depends on

**The scheme is well-balanced over ARBITRARY topography, and that is load-bearing.**
`_rhs` uses Audusse et al. (2004) hydrostatic reconstruction: interface bed
`max(z_L, z_R)`, free surface against the cell's own `z`, and a face-pair bed-slope
source. A lake at rest is preserved to machine precision on flat, linear, one-cell
bump, one-cell wall, one-cell notch, diagonal-wall and real conditioned terrain.
Measured 2026-09-13: free-surface spread 2.8e-14 m, |v| 1.8e-14 m/s.
*Pinned by:* `tests/test_well_balanced.py` (asserts on `_rhs` itself, not on a
replica of its internals). *Do not* reintroduce a cell-mean-preserving wet/dry tilt:
with a max interface bed it fires for every wet cell adjacent to a barrier and
destroys the state it was meant to protect.

**The bed-slope source must vanish identically on a flat bed.** It is
`(g/2)[(h*_p² − h_edge_p²) − (h*_m² − h_edge_m²)]`, the difference between each edge
depth measured against the FACE bed and against the CELL's bed. Writing it as
`(g/2)(h*_p² − h*_m²)` is correct at rest and wrong everywhere else — MUSCL makes the
two edge depths differ on a flat bed, so the term reads a depth gradient as a bed
gradient. Measured cost of getting this wrong: Ritter RMSE 1.379 m against a 0.043 m
bar, front error 61 %. *See:* `findings_results.md`, 2026-09-13.

**The bed-slope source acts only on wet cells.** A dry cell has no water column and
no bed reaction. Without the `h_c > _DRY` mask, real terrain carried up to 2.0e+02 of
spurious `dhu`/`dhv` on dry cells while `dh` stayed exactly zero — momentum that
could not move water immediately, but sat waiting to become velocity the moment the
flood wetted the cell.

**A barrier holds at ANY thickness, including one cell.** Measured 0.000 m³ passed at
1, 2, 3 and 5 cells over 900 s with the pool 5 m below the crest and friction off, on
both a flat-floored and a sloping-floored reservoir. This was NOT true before
2026-09-13 — a one-cell wall passed 55.4 % of its pool in 300 s, because the
mean-interface bed left no face anywhere at the crest.
*Pinned by:* `tests/test_barrier_integrity.py`.

**`z` must be copied into the solver, not aliased.** The breach opening mutates the
bed in place. `np.ascontiguousarray` returns the input unchanged when it is already
contiguous, which silently mutates the caller's DEM.

**`clipped` and `bed_lowering` are different quantities and must never be merged.**
`clipped` is manufactured mass (a failure signal, gate G2). `volume_bed_lowering_m3`
is storage *re-attribution* when the bed drops under standing water (legitimate,
accounted separately). Folding one into the other is what made the old mass gate a
tautology.

**Injection and opening are mutually exclusive.** Supplying both re-creates the C1
double count. `run_2d_swe_simulation` raises rather than accepting both. Do not
"keep the injection for comparison".

**Ghost cells are the *window* edge, not the domain edge.** `_rhs` runs on the active
window, so `np.pad(..., mode="edge")` replicates window boundaries. Reasoning about
boundary behaviour from domain geometry alone is wrong.

**Q is measured, never prescribed** (central arm). The control volume is
`upstream_basin_mask | barrier_mask`; its storage derivative, corrected for bed
lowering, is the discharge. The 0-D routed hydrograph is a **check**, not a driver —
and must not be tuned to agree with the measurement. The disagreement is the finding.

**A scenario that cannot source its crest or dam axis fails the geometry gate.**
No defaults. `wse_m + 5.0` is gone and must not return — a dam whose crest is "5 m
above whatever level we assumed" is a fabricated dam.

**An OSM cache keyed on filename alone is stale the moment a bbox moves.**
`build_rivers` / `build_facilities` / `build_buildings` return their cached GeoJSON
unless `force=True`, and nothing in the filename records which AOI it was fetched
for. `annamayya_rivers.geojson` was the **2026-09-05** fetch, from before the bbox
was extended south to 13.85 — so it had never contained the Pincha arm, and every
consumer got that silently. `_bbox_stamp_ok` now writes a `<name>.bbox.json` sidecar
and a cache with no sidecar counts as UNKNOWN, not current.
**`build_villages` is deliberately exempt**: that file IS the population layer and is
hand-curated, so a silent refetch would delete the corrected Nandalur coordinate, the
reconciled population and the Mandapalli provenance. It warns instead.
On a fetch failure `build_rivers` returns the stale layer labelled
`attrs["bbox_stale"] = True`; `locate_handoff` and the terrain-conditioning path
**refuse** that label, because they site a physics boundary and carve terrain from it.

**`u` is the velocity along +col and `v` along +row — and +row is SOUTHWARD.**
`_rhs` pairs array axis 1 (columns) with `hu` and axis 0 (rows) with `hv`
(`_rusanov(..., normal_axis=(0 if axis == 1 else 1))`). On a north-up raster
`transform.e < 0`, so a **northbound** river has `v < 0`. Any flux taken against a
direction expressed in UTM eastings/northings must convert first:
`d_col = tx*sign(tr.a)`, `d_row = ty*sign(tr.e)`. Projecting a UTM tangent straight
onto `(u, v)` measured **−5.75 MCM** through the Annamayya handoff section on the
12 h Stage-1 run of 2026-09-13 — the sign was what exposed it. A section oriented
mostly east-west would have given a wrong magnitude with no sign to catch it.
*Pinned by:* nothing. `route_annamayya.py::locate_handoff` carries
`flow_direction_grid` for this reason; a second consumer should reuse it, not
re-derive it.

**Every parameter needs a causal path to the solver and the output.** A knob that
changes no output is a defect, not a feature. Applies to the whole repo; it is the
project's core honesty constraint.


**A settlement coordinate is validated against terrain, not against a geocoder.**
Nominatim's "Nandalur" is 11.76 km from the OSM `place=village` node and sits at
**HAND 141 m** — a town whose river bridge washed out cannot be 141 m above the
nearest drainage. The village layer was briefly recorded as 11.7 km wrong on the
strength of that geocode; it was the geocode that was wrong. Cross-check any
settlement position against HAND before trusting it.
What does survive: the layer's Nandalur (79.1200/14.2580) is at HAND 0.4 m, i.e. in
the channel, 1.89 km from the town node (79.1080/14.2704). The run reports 2.55 m at
the layer point and **0.24 m at the town**, and it is the layer point that drives the
EVACUATE rank #1 call.
*Pinned by:* nothing yet. A HAND-based sanity test over the whole layer would be cheap.

**`validity.geometry == "not_applicable"` is a real verdict, not a missing one.**
`is_valid` accepts it only when `run_type == "FORCED_HYDROGRAPH_INUNDATION"` **and**
`impoundment_modelled is False` **and** the string is exactly `"not_applicable"`.
`geometry: False` is still a refusal, and a dam-break run carrying it is still
refused. Do not widen this branch to make a scenario render.
*Pinned by:* `tests/test_forced_hydrograph_validity.py` (10 tests).

## 3. Traps — looks like a bug, is not

**A job restored from disk is not the same object as a live one, and reconcile will not
save you.** `_JOBS` is in-memory. After a restart `_rehydrate_saved_scenarios` rebuilds
each job, and `_reconcile_job` SHORT-CIRCUITS on any job that is already `done` with a
manifest - which a rehydrated one always is. So any field rehydration forgets is never
filled in for the life of the process. This produced 404s on `/api/roads`,
`/api/envelope_geojson` and `/api/arrival`, and a 500 on `/api/manifest`, all with the
files present and registered. Both paths now build their fields from
`_job_fields_from_result`; add a field there, not in one caller. Pinned by
`tests/test_job_rehydration_is_complete.py`.

**Progress crosses a process boundary.** `execute_full_simulation`'s `progress_cb` writes
to memory in whichever process runs the pipeline, and for an API run that is
`src/api/worker.py`, not the server. The worker mirrors each callback into
`progress.json` beside the manifest and the API reads it back. Do not "simplify" this by
having the API read the callback directly - there is nothing to read. Do not write
progress into the manifest either: that file is hash-verified provenance.

**`is_valid()` includes the PHYSICS verdict, not just manifest integrity.** It requires
`validity.valid is True` and `geometry`/`physics`/`sources` all True, so a run whose gates
refused it is never served - by design, because publishing an inundation map that failed
its own gates is the one thing this project must not do. The cost is that with 0 of 7
scenarios passing, only legacy runs are servable. That is a known, deliberate state; see
`memory.md`, "Nothing produced today can be displayed".


**`--wse` raises, and so does the API's `wse_m`. That is deliberate.** The water level
is NOT settable. `execute_full_simulation` refuses any non-`None` `wse_m` with a
`ValueError` naming the reason, and `POST /api/run` returns **422** for a supplied
`wse_m`. A flag that exists and refuses looks like a bug and is not.

Why it is refused rather than honoured: the level must be DERIVED from the sourced
`thalweg_m + dam_height_m` (or the cascade reservoir's `z_crest_m`) and BOUNDED by the
sourced `crest_elev_m`. A caller-supplied level is a level floating free of its
sourcing, which is the defect the 2026-09-18 crest clamp removed - a level 1.23 m above
the crest cost a 46x volume blow-out.

Why it is refused rather than deleted: **Pydantic drops unknown keys by default**, so
removing the request field would leave a client still sending `wse_m` having it dropped
in silence - the same no-op, relocated to the HTTP boundary. Keeping the field with a
`None` default lets the endpoint say why.

Until 2026-09-18 it was silently discarded: never read between function entry and the
two points that overwrite it (`run_pipeline.py:549` and `:552`). Three things fed that
void - the CLI flag, which additionally fell back to the scenario's own `wse_m` so a
concrete level was ALWAYS passed; `src/api/main.py`'s field, which defaulted to
**3850.0**; and `src/api/worker.py`'s forwarding. Because `/api/run` writes
`req.model_dump()` into the run manifest, **archived manifests record a level their run
never used** - `101520ad...`'s request says 3850.0 against a run that used 3805.11.
Those manifests will now raise if replayed, which is the point.

`frontend/index.html` had already removed its "Water level" box for this reason and
documents it there. Pinned by `tests/test_wse_m_is_refused.py`. The level a run actually
used is reported as `validity.initial_wse_m`.


**`test_lake_at_rest_is_still` used to prove nothing, and now does.** Its lake drained
off the grid — 0.002 % retained — so the velocity sample was taken over dried cells.
That drain-away turned out to be a SYMPTOM: the lake was being pushed off the grid by
the scheme's own spurious currents (~40 m/s), not flowing out under its own weight.
Since the Audusse fix the same open-boundary terrain retains 100.000 %, so the test
now has a lake to test. It was kept rather than deleted for exactly that reason.

**0 of 7 scenarios valid is not a regression.** The gates stopped being tautologies
on 2026-09-12, and G5 was added on 2026-09-13. Before that they could not fail. A
scenario count going *down* after a gate change is the gate working.

**A breach narrower than one cell never opens, and used to do so silently.**
`run_2d_swe_simulation` applies width as `mask & (axis_distance <= width/2)`. phutkal
at coarsen 4: a 37.7 m breach against 111.6 m cells opened 0 cells and measured a
peak discharge of exactly 0 m³/s, while the report said `active: true` and G2/G3
passed. The same breach at coarsen 1 opens 10 cells. Now refused at construction.
The fix for a sub-grid breach is a finer grid, never a wider breach.

**G5 exists because every other gate can pass on a run where no water moved.** G1
checks where the water came from, G2 that none was invented, G3 that an exit exists,
G4 that the pool is held. None of them asks whether the dam broke.

**Near-breach velocity of ~30 m/s and Fr 5–7 is physical, not numerical.** Confirmed
by two controlled sweeps: `v/√(2g(H+drop))` is a stable ~0.55, and holding the drop
fixed while flattening the bed curvature leaves velocity unchanged. Do not cap it.
The old 67 / 330 m/s figures belonged to the deleted injection.

**Measured Q peaking at 2.7× the routed value is the first ~5 seconds only.** A
storage-derivative artefact over sub-second startup steps. The measured peaks
reproduce exactly (550 795 at t≈0.4 s, 122 767 sustained); the RATIO does not — the
0.603 figure depends on a 0-D routing that is not committed, and an independent
routing gives 0.830. Both are below 1, which is the physically expected direction.
Do not quote 0.603 as a reference until both curves come from committed code.
The measured curve is now exported to `hydrograph.json` under `"measured"`.

**The outlet is not the reason so little water leaves.** Its cells are flat,
contiguous, and carry no interface-averaging penalty; `width_cells` 1→3 changes the
escape level not at all. The limiters are 13–28 m of residual ponding and the fact
that **1 of 820 boundary cells is open**. Do not widen or lower it.

**`max_depth.tif` containing the reservoir is deliberate.** The map should show the
pool. Consequences read `flood_depth_grid` instead (P4).

**The envelope arms no longer inject** (fixed 2026-09-13). All three arms get their
own `BreachOpening`, built from that arm's `BreachParams`, and their injection
hydrographs are zeroed. `envelope.tif` and `envelope.geojson` no longer carry the
2.00× C1 double count.

**GPU is slower here.** The cupy path measured 6.6× slower on this GTX 1650. Do not
re-propose enabling it.

**A percentile floor is set by the artefact it exists to catch.** `condition_dem` used
`floor = percentile(valid, 0.1) - margin_m`. On the Annamayya grid at coarsen 5 — which
takes the block MINIMUM, so zeros propagate — `percentile(valid, 0.1)` **is 0.00 m**,
because the zero-fringe cells are themselves in `valid`. Floor landed at **-100 m** and
walled none of them: 279 cells at bed 0.00 m, 11.38 MCM drained into them, 14.19 MCM
left across the adjacent boundary — 22 % of the flood disposed of by a DEM fringe.
Exact zeros are now flagged *before* the statistic is taken. **Excluding them is not
enough on its own** — it lifts the percentile to 64.79 m but -100 m of margin still
buries the floor at -35.21 m, leaving 97 cells below 60 m. A low-lying domain needs a
sourced `floor_m`.
*Pinned by:* `tests/test_dem_conditioning_floor.py`, which asserts
`percentile(raw, 0.1) == 0.0` as a precondition so it cannot pass for the wrong reason.

**`thalweg_m - margin` is NOT a domain floor, and looks like one.** `thalweg_m` is the
DAM-SITE bed. Any domain that runs downstream far enough drops below it. Measured
2026-09-13 at the `run_pipeline.py` `condition_dem` call site, cells of REAL terrain
that `thalweg_m - 100` walls: rishiganga **34,983**, phutkal **2,186**, south_lhonak
**2,649,346** (real valley floor 715 m against a 5,140 m thalweg). It also walls
**905 interior cells of real Penna floodplain** on the widened Annamayya domain. It
broke `test_run_lifecycle.py::test_crest_gate_rejects_wse_above_dem_barrier`, which is
how it surfaced. Use `dem_floor_m`, measured per domain; Annamayya's 60.0 m sits on a
**29x** population step in the DEM ([50,60) holds 104 cells, [60,70) holds 3,037).

**There is NO unmapped gap between Pincha and the Cheyyeru — that was a stale cache.**
An earlier entry here and in `findings_results.md` reported "the nearest mapped river
to the Pincha ring bund is 8.84 km away" and built a sheet-flow hypothesis on it.
Wrong, and retracted. OSM maps the **Pincha River** (way 148491940, 54.57 km) and it
passes **0.11 km** from the ring bund; the 2026-09-05 river cache simply predated the
southern bbox extension. The Cheyyeru's own head really is 8.84 km away, for a
different reason: **the Cheyyeru BEGINS at Rayavaram (13.9883/78.9926)**, where the
Pincha and Bahuda headstreams merge. 8.84 km is a distance to a confluence, not to a
river. Its other headstream, the **Bahuda, is outside the domain** — 4 OSM ways, only
9 of 73 points of the nearest inside the bbox — and is entirely unforced.

**The domain was SEALED and nothing said so.** `route_annamayya.py` never called
`open_river_outlets`, which `run_pipeline` does, so every run measured
`outflow 0.000e+00` and every extent was an upper bound on ponding. It also makes the
plan's own convergence criterion — "a plateau in wetted area with FALLING OUTFLOW" —
untestable by construction. With the corrected river layer (which now contains the
**Penna**, absent before) `--open-outlets` opens **5 outlets**, beds at 62.0 / 96.5 /
96.83 / 126.27 / 143.13 m. Off by default; turning it on is a deliberate, reported act.

**Pincha has its OWN DSM water plane, one reservoir upstream of Annamayya's.** 8 cells
at exactly **294.50 m**, 0.19 km2, at 13.9045-13.9087 / 78.9997-79.0011. Same artefact
as the 192.50 m plane at Annamayya, same cause: GLO-30 is a surface model captured with
the reservoir full. A release injected at the Pincha coordinate is being injected onto
a water surface, and the nearest **mapped** river is **8.84 km** away. Do not read a
Stage-1 travel time as a channel travel time.

**An arrival read from a run with a non-dry initial condition is a FALSE PASS.**
`compare_arrivals` records an arrival the first time a frame shows depth > threshold at
the point. If the run STARTED with water there, that is frame 0 and it scores 0.0 min.
Measured on `annamayya_stage2_wetchannel`, whose 18 h spin-up left 51.86 MCM standing
over 47.1 km2: four of the five EVD points begin under 1.08-2.16 m of water, all four
report 0.0 min, and **EVD-23 scores MATCH** -- an arrival match the model did not earn.
Run `scripts/check_prewet.py` before quoting an arrival from any run with a spin-up or
an `initial_depth`, and read only the points it reports dry at t=0.

**18 h of the sourced 800 m3/s baseflow does not flow -- 99.4 % of it just sits there.**
51.84 MCM injected, **51.86 MCM still standing, 0.33 MCM out through 5 open outlets**.
A real channel carrying 800 m3/s conveys it downstream and out; this one spreads it over
47 km2 and holds it. This is the conveyance deficit stated as a mass balance, and it is
the same defect as the slow front. It also means antecedent-flow initial conditions
cannot be built by spin-up until conveyance is fixed: the spin-up converts a sourced
DISCHARGE into unsourced STANDING WATER.

**The front is ~3.3x too slow, and flowline/outlets/roughness do NOT explain it.**
Measured on Stage 1, same forcing, same grid: arrival at the handoff moves
**+210.0 min -> +175.1 min** (17 % better) when `--condition-flowline`,
`--open-outlets` and `--channel-roughness` are all switched on together, and peak Q
1,190.8 -> 1,309.3 m3/s. The gate needs roughly **3.3x** faster, not 17 %. Those three
were the leading hypotheses in the 2026-09-13 entry and together they close about a
twentieth of the gap. Do not re-propose them as the fix. The remaining candidate is
grid resolution -- a 50-100 m channel is sub-grid at 152 m cells, so its conveyance is
a channel-and-bank blend -- and that is what the coarsen sweep tests.

**Matching the `corridor` layer is VOLUME-INFEASIBLE, and it is not ground truth anyway.**
The corridor union inside the widened domain is **378.84 km2**. Wetting it to the
project's own 0.30 m flood threshold takes **113.7 MCM** against the **101.5 MCM** the
sourced hydrograph releases -- and that is with zero depth anywhere above threshold,
while the run holds 13.41 m in the channel. At the run's realised mean depth it would
take **~250 MCM, ~2.5x the sourced water**. The corridor is three HAND bands off the
same DEM the model runs on, its outer ring is ground **5-10 m above the nearest
drainage**, and no satellite observed this flood. Coverage against it is a diagnostic
for reach and truncation. Never tune to it, never call it validation, never score a
CSI on it.

**A per-village statistic sampled at the polygon CENTROID is not a village
statistic.** Fixed 2026-09-14. `route_annamayya.py` read depth and arrival from the
single cell under each polygon's centroid — 2.32 ha at 152 m, standing in for a
settlement kilometres across. Measured on `annamayya_stage2_wide`: **11 of 23
settlements reported 0.00 m with part of their polygon under water**, and **inundated
went 10 -> 21, PAR 19,950 -> 30,647** once masked properly. Worst cases: Ramachandrapuram
4.44 m over 94 % of it, Obili 3.13 m, Gundlur 3.11 m. **Gundlur is the one that matters:
EVD-25 reports 2.0-4.0 m there, the model produced 3.11 m, and the centroid sample threw
it away — a reproduced observation was reading as a miss.** `m5_exposure.
compute_village_exposure` always did this correctly and is what `run_pipeline` uses; the
centroid version was a parallel reimplementation of it.
*Pinned by:* `tests/test_village_exposure_polygon.py`.

**Mask villages with `all_touched=False`, matching `rasterio.mask.mask`.** A cell counts
when its CENTRE is inside the polygon. `all_touched=True` adds a one-cell ring to every
settlement, which at 152 m is ~150 m of borrowed ground — measured on Gundlur it moved
flooded fraction **33 % -> 64 %**. The two code paths must agree or the same village
scores differently depending on which one ran. A settlement smaller than one cell is
flagged `SUBCELL` and given the cell it sits in, rather than reported dry.

**A settlement badge keyed on ARRIVAL TIME calls flooded villages safe.** Fixed
2026-09-14. `map.js` decided the badge with
`isAtRisk = isIsolated || (!isMissing(waterMin) && +waterMin < 120)` — an arbitrary
two-hour cutoff, with `max_depth_m` never consulted at all. Anything the flood reached
at T+120m or later fell through to a green **SAFE**. Measured on
`annamayya_stage2_wide`: **six settlements under water carried a SAFE badge**, including
**Paparajupalle at 3.78 m, T+150m**. The clincher is Seshamambapuram, the same village in
two runs — T+118m in one, T+122m in the other — which flipped WATER → SAFE on four
minutes while taking 2.16 m either way. Danger is depth; arrival time says *when* to
move, never *whether*. Rule now lives in `frontend/village_status.js`.
*Pinned by:* `tests/test_village_status.mjs`, which asserts on that function and carries
all six villages as named regression cases; `tests/test_village_status_node.py` runs it
under pytest so it cannot rot.

**There is no "SAFE" badge, deliberately — dry renders as NOT REACHED.** A settlement a
run never wetted has not been shown to be safe, it has been shown not to be reached in
*this* run, and this run's front is ~3.3x too slow over a domain that barely drains.
Grey states that; green would claim more than the model supports. Do not reintroduce a
green SAFE, and do not "fix" a NOT REACHED marker by lowering the depth threshold.

**`observed_wse` is NOT ground truth either, despite the name.** It replaced the corridor
as the map's purple layer on 2026-09-14. Its water LEVEL is reported -- three high-water
depths, EVD-23/25/26 -- but where that level meets the ground is read off the same
Copernicus GLO-30 the solver integrates over. The arbitrary half of HAND is gone; the
circular half is not. Same rules: never serve it through the `observed` kind, never score
a CSI on it, never tune to it. The validation target is the five arrival records.

**`observed_wse` covering only 8.74 km is deliberate, not a truncated run.** The
reconstruction refuses to extrapolate past its outer anchors, and no reported depth exists
downstream of Nandalur -- EVD-24 has none and EVD-28 gives arrival timing only. A map that
stops where the evidence stops is the intent. Do not "fix" it by extending the surface.

**A reported depth is depth over the ground AT THE PLACE NAMED, not over the channel bed.**
`station_anchors()` samples the ground under each cited coordinate. Anchoring to the bed at
the projected station instead is measurably wrong: Gundlur sits 618 m off-channel on a
terrace 6.8 m above the bed, so bed + reported depth put the village 2.8 m ABOVE its own
reported flood. Pinned by the acceptance check that every anchor must fall inside the LOW
band, which is what caught it.

**The sourced Pincha->Annamayya distance is a straight line wearing a river's name.**
`pincha_to_annamayya_distance_km = 34.0`, classified OBSERVED, sourced to "Survey of
India GIS River Line / HydroRIVERS". The straight line is **33.46 km** — 1.6 % away.
The mapped OSM path is **43.42 km**, 28 % longer. Every celerity derived from the
sourced 120 min transit inherits this: 4.65 m/s on the straight line, **6.03 m/s** along
the channel. Do not treat the 4.5 m/s wave speed as an independent check on a routed
arrival time.

**Annamayya's dam is not in the 30 m DEM.** Moving the breach coordinate cannot fix
it. The DEM holds a flat 192.50 m water plane.


**A settlement reading 0.00 m in `annamayya_routed` means "not reached yet", not
"safe" — but on the WIDENED 24 h run that reading has changed.** The old entry was
right about the old run: 8 h cap, wetted area still growing ~7.2 km2/h, no plateau,
`stopped_early` False because it hit the clock, eight of eleven dry settlements
0.49-2.29 km from the Cheyyeru.
Amended 2026-09-14 with the measurement it was waiting for. On
`annamayya_stage2_wide` (24 h, widened domain) the wetted-area growth rate runs
10.21 km2/h at t=4 h, **falls to -0.36 km2/h at t=12 h**, and the front reaches
**100 % of the 54.0 km stem**, stopping 2.6 km short of the boundary. The release ends
at 8 h. So the 13 dry settlements are now better read as **outside this flood's
extent**, not as un-reached — with the caveat that keeps this from being a clean
"safe": the domain still cannot drain where the flood sits, and a residual
**1.7 km2/h** of slow floodplain redistribution continues to t=24 h. That is water
spreading sideways, not a front advancing.
Still do not "fix" a dry marker by lowering the depth threshold.

**"Flooded area" for these runs is a 24 h ENVELOPE, not a snapshot, and the two differ
by 1.75x.** `annamayya_stage2_wide` reports `flooded (>0.3 m): 153.00 km2` — that is
`max_depth_grid > 0.3`, the union of everywhere that was ever wet. The **peak
instantaneous** wetted area is **87.70 km2**, and the peak of the first
(release-driven) rise is **78.01 km2 at t=10 h**. Quote which one you mean: a headline
extent that silently mixes them overstates the flood at any given moment by ~75 %.

**Absence of a marker is absence of evaluation.** The population layer holds 23
settlements; the affected area contains well over a hundred named villages. Ground
with no marker was never scored. The UI cannot currently express this and renders it
indistinguishably from cleared ground — see `ui.md`.
**Sharpened 2026-09-14:** not-reached settlements are now not drawn at all, so an empty
patch of map means "scored and dry" OR "never scored". Deliberate — they carry no
operational instruction and there are only two — but it widens this exact gap. All 23
remain in `results.geojson` and in the ranked list. A future fix is a distinct
"scored, dry" symbol; it is NOT a green SAFE badge, which is what was removed.

**No satellite observed the Annamayya flood, so any "observed extent" for it is
fabricated until proven otherwise.** Sentinel-1 path 92 is the only footprint over
the flooded reach and acquired 16 Nov then 28 Nov — the event sits in a 12-day gap.
Path 165 flew 21 Nov (+48 h) but its data footprint ends near 78.93E, ~9 km west of
the dam, and contains none of the affected villages (point-in-polygon tested).
Sentinel-2 passed 4 h after the breach into 98.7 % cloud over the corridor. The
deleted `annamayya_observed_extent.geojson` was unattributed for this reason. The
best available observation is Sentinel-2 24 Nov (+5 d, 61.2 % clear) differenced
against 4 Dec: **residual standing water, 16.96 km2**, a lower bound on ponding that
includes rain-filled paddy and tanks. Never label it peak extent, and never quote a
CSI against it as skill (it computes to 0.029 — the quantities differ).

### Per-STRUCTURE manifests vs per-SCENARIO everything else (south_lhonak)

Geometry manifests are keyed per **structure**; DEMs, `SCENARIOS` entries and OSM
extracts are keyed per **scenario**. The two coincide for every single-structure
event, so the distinction was invisible until south_lhonak — a compound event
authored as `south_lhonak_chungthang.json` + `south_lhonak_moraine.json`, with
nothing under `south_lhonak.json`.

**Fixed 2026-09-19 in `scripts/author_crest_elevations.py`** via
`scenario_key_for()` (longest-prefix match). Before it, that script looked for
`south_lhonak_chungthang_dem.tif` and reported "no DEM" for a scenario whose DEM
has been on disk since 2026-09-06.

**TRAP — do NOT apply the same fix to `scripts/generate_geometry_manifest.py`.**
It looks like the identical bug and is not. That script authors geometry *from*
`SCENARIOS[key]["breach_lon"/"breach_lat"]`, and south_lhonak carries **one**
breach coordinate — (88.18742, 27.91478), the moraine-dammed lake at ~5,200 m.
Mapping the structure keys onto the scenario key would hand **both** manifests
that single coordinate and then `main()` writes straight to
`data/geometry/<key>.json` with no dry-run. Chungthang's geometry (barrier floor
**1,510.79 m**, ~42 km downstream) would be silently overwritten with the
moraine's (barrier floor **5,194.29 m**), destroying the two-structure
distinction and leaving two manifests that look authored and describe the same
place.

As it stands the generator fails safely: `KeyError: 'south_lhonak_chungthang'`.
Leave it failing. A compound event needs a breach coordinate **per structure**,
which the `SCENARIOS` schema does not carry — that is a schema change, not a
lookup fix.

Related: the moraine's crest refusal is caused by the same per-scenario/per-
structure collapse. `dam_height_m = 60.0` is scenario-level and
`downstream_structure.height_m = 60.0` confirms it is **Chungthang's** height;
applied to the moraine it derives a crest (5254.29 m) above the moraine's own
highest barrier cell (5222.18 m), so the crest script correctly refuses.

## 4. Shortcuts with a known ceiling

- `escape_head_4connected` is a minimax search, O(n log n) per call; fine at these
  grid sizes, would need a proper priority-flood if domains grow.
- Q is measured as a control-volume derivative, not a face-flux sum. Exact for a
  conservative scheme, noisy on the first step. Upgrade path: return per-face `F_h`
  from `_rhs` and sum over the opening's downstream faces.
- The 25 m refusal cap in `condition_flowline` is uncalibrated — no surveyed cross
  sections exist for these reaches.
