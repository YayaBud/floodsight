# Problem-statement deliverables — what is in scope, and what is not

**Date:** 2026-09-12, **amended 2026-09-14**. Written because four of the six named
Indian events have no scenario at all and two deliverables were being presented in
the UI as working when they were not. Everything below is a status, not a plan.

The 2026-09-14 amendment changes two rows: Annamayya now produces a complete run (a
forced-hydrograph one, and it says so), and deliverable (iii)'s .shp/.kml exports now
cover that run. Deliverables (i) and (iv) are unchanged — Delft3D has still never been
run, and GEE is still not wired.

---

## Deliverable (i) — comparison against other solvers

| Arm | Status |
|---|---|
| FloodSight 2D FV SWE | runs on every simulation |
| SWE-SPH 1D, Ritter analytical benchmark | **runs live**, result is real |
| SWE-SPH 1D, per-scenario thalweg comparison | **not run** — refused, reason below |
| Delft3D-FLOW | **never run in this project** |
| ANUGA | module deleted 2026-09-12 (unreachable, zero callers) |

**Why the SPH scenario comparison is refused** (`src/m4_solvers/sph_swe.py`, and
shown verbatim in the Solvers tab). It is not a like-for-like comparison:

1. **Forcing.** After P3 the 2D arm has no forcing hydrograph — the pool drains
   through an opening cut in the barrier and Q is an *output*. The 1D arm would
   be driven by the 0-D routed hydrograph, so the two solvers are not being
   given the same event.
2. **Spatial support.** 1D particles on a thalweg polyline against cell-averaged
   depth on a 28–113 m grid. In a gorge narrower than one cell these are
   different physical quantities, and an RMSE between them is not a
   solver-agreement number.
3. **Comparison times.** The 2D arm reports a run maximum; the SPH arm reports
   its own final instant.

To make it real: drive the 1D arm with the 2D run's **measured** breach
discharge, compare at matched times, and declare a per-station channel width so
the 2D arm can be sampled over the same width. ~85 lines of unreachable
implementation sat below the early `return`; it was deleted rather than left
dormant.

**Delft3D.** The Solvers tab displayed "Ritter RMSE 0.14 m, precomputed". No
code in this repository produced that number and no run is on record behind it.
It has been **removed**, not relabelled. The card now says "not run in this
build" and reports no result.

---

## Deliverable (iii) — dashboard, and .shp / .kml output

**Status: delivered.**

The GUI is `frontend/` — map, timeline scrubber, depth scrubbing over saved frames,
per-settlement consequence panel, validation layers.

Export: `src/m8_outputs/exporters.py` writes ESRI Shapefile (plus a `.zip` with its
sidecars and a `_fields.json` recording every name truncated to the 10-character
limit), KML, and a CAP-conformant JSON alert payload.

**Fixed 2026-09-14.** `run_pipeline.py` had always called the exporters;
`scripts/route_annamayya.py` — the script that produces the one Indian run actually
demonstrated — never did. So the flagship run had no `.shp` or `.kml` on disk while
the deliverable names those formats explicitly. `route_annamayya.write_exports()` now
writes **two** layers, because "the output" means two different things:

| layer | what it is |
|---|---|
| `*_settlements` | the ranked consequence table, 23 features — what `run_pipeline` exports |
| `*_inundation` | the flood extent itself, as depth-class polygons — what a GIS user usually means |

Both carry a `provenance` block, archived inside the shapefile zip, stating that this
run PRESCRIBES its release. A shapefile that escapes into someone's GIS with no caveat
attached would read as a modelled dam-break result, which it is not.

Backfilled onto `annamayya_stage2_wide` and registered in its manifest, so
`/api/download/{job}/{fmt}` serves them.

**A defect this surfaced, fixed at the root.** `export_kml` crashed on
`int(row.get('buildings_flooded', 0))` — the column *exists* and holds `None`, so the
`.get` default never fires. Annamayya has no OSM building layer for its AOI. Fixed in
`exporters.py` for every caller, not in the new one: `_as_int` treats None/NaN as
absent, and `_count_or_note` prints *"not computed (no OSM building layer exists for
this AOI)"* rather than a confident `0`. A missing count and a measured zero must not
render identically in a file that leaves this system.

---

## Deliverable (iv) — near-real-time framework via Google Earth Engine

**Status: framework present, NOT WIRED.**

`src/gee_satellite.py` is a real Earth Engine / Sentinel-1 module with its own
tests. It is imported by nothing except `tests/test_lake_cascade_gee.py`. No GEE
query runs anywhere in `run_pipeline.execute_full_simulation`.

The frontend's "SAR (GEE)" toggle and its "Sentinel-1 SAR (GEE)" legend entry
have been **removed**. Its map source was a permanently empty
`FeatureCollection` — nothing ever populated it. The five ~1 KB
`data/satellite/*_sentinel1_sar.geojson` files that appeared to correspond to it
were hand-authored polygons, not Sentinel-1 products, and are deleted.

To make it live: a caller in the pipeline supplying an AOI and acquisition
dates, plus a credentialed Earth Engine service account. Neither exists here.

---

## Deliverable (v) — Indian scenario scope

The problem statement names six events.

| Named event | Scenario | Status |
|---|---|---|
| Rishi Ganga, Uttarakhand — Feb 2021 | `rishiganga` | exists. Passes the structural geometry gate; **fails validity gate G4** — its configured water level 2175.68 m is **12.79 m above** the 2162.89 m at which water escapes its own basin. Also a debris flow, which the clear-water solver refuses by design. |
| Phuktal river near Sumdo — Mar 2015 | `phutkal` | exists, runs end-to-end, **reports `valid: false`**. Updated 2026-09-18: **G1, G2, G3 and G5 now pass**; only **G4** fails. G1's old failure was a units defect — `reservoir_fill` was read as a VOLUME fraction in one place and a STAGE fraction in another, so the initial condition held 0.8044 of the pool where the gate expected 0.9. Fixed; G1 now passes at ratio 1.0000034. **The previously recorded "DEM pool holds 2.97e6 m³" does not reproduce at any resolution, level or barrier state** and belongs to a superseded configuration (crest 3794.11 m, `wse_m` 3878.0 m); it should not be quoted. G4 fails because the upstream basin spills at 3777.67 m, **27.44 m below** the 3805.11 m crest at 55.8 m cells — and **17.91 m below it at 27.9 m cells, the native DEM resolution**, so resolution cannot close it. phutkal's configured 27–30 MCM does not fit its own basin in this DEM. Date note: the blockage formed 31 Dec 2014 and breached 7 May 2015, not March. |
| Kosi river — 2008 | **none** | No scenario. `data/validation/gfd_dam/DFO_3382_From_20080922_to_20080929.tif` (39 MB, Global Flood Database / DFO, Tellman et al. 2021, CC BY-NC 4.0) **is already on disk** and is the shortest path to the first real Indian observation. Needs: a DEM AOI, an OSM river extract, a breach coordinate, and a geometry manifest with a sourced `crest_elev_m`. |
| Kashmir Valley — 2014 | **none** | no scenario, no data on disk |
| Assam — 2014 | **none** | no scenario, no data on disk |
| "Wapriyang river" — Nov 2021 | **none** | **Identified 2026-09-24** (earlier "could not be identified" was wrong): Warriyang Bung / Wapra Bung, a Kameng tributary in East Kameng, Arunachal Pradesh. A landslide on a glaciated slope caused debris flows on 29 Oct and 31 Oct–4 Nov 2021, silting the Kameng; radar showed no lake by 7 Nov (Arunachal Times, 10 Nov 2021; AGU Landslide Blog, 1 Nov 2021). It is a **debris flow**, which the clear-water solver refuses by design — out of scope for physics reasons, not for lack of identification. |

**Four of six named events have no scenario. Of the two that do, neither
currently produces a valid run.**

### Scenarios present that are outside the named Indian scope

These are legitimate cases and must not be presented as substitutes for the
named events.

| Scenario | Country | Status |
|---|---|---|
| `derna` | Libya | The only scenario with a certifiable observation (Copernicus EMS EMSR696). **Now fails the geometry gate**: its `crest_elev_m` could not be sourced — the DEM-derived crest (261.20 m) exceeds its own barrier abutments (217.96 m), which means the dam axis did not resolve on the structure. It is a validation case, not an Indian demonstration. |
| `malpasset` | France | Numerical benchmark only. Crest sourced (110.75 m); still fails the gate on seed separation. |
| `ivanovo` | Bulgaria | Its only "observation" was fabricated and has been deleted; unregistered from `observed.SOURCES`. Fails the gate on seed separation. |
| `annamayya` | India (not named) | **Amended 2026-09-14 — this row was stale.** Best-documented Indian dam-break case here. Crest is the one **published, cited** figure in the repository: 206.0 m MSL, Technical Expert Committee Report (2021) / Restoration DPR (2022). It still fails the dam-break geometry gate, but no longer for the reason given on 09-12: the "no OSM river cleared the barrier" finding came from a river cache that predated the southern bbox extension. The real blocker is terrain — GLO-30 over this AOI is a DSM captured with the reservoir full (a flat 192.50 m water plane against the 206.0 m crest), so the dam and the valley floor beneath the pool are simply absent and the impoundment cannot be emplaced or confined. It therefore runs `scripts/route_annamayya.py` as **`FORCED_HYDROGRAPH_INUNDATION`, `impoundment_modelled: False`** — the release is prescribed from sourced figures and routed, rather than drained through a breach. `annamayya_stage2_wide` is complete and artifact-backed: **153.00 km² envelope** (peak instantaneous 87.70 km²), max depth 13.41 m, 10 settlements inundated, 19,950 PAR, **mass closure 2.15e-08**, `physics: True`, `sources: True`, `geometry: not_applicable`. Known limits carried in its own `validity.reasons`: discharge is an input not an output, the impoundment is not modelled, and the domain is effectively sealed so the extent is an upper bound on ponding. The front is also ~3.3× too slow against the arrival gate. |
| `south_lhonak` | India (not named) | No geometry manifest under its own key; the two that exist (`_chungthang`, `_moraine`) have no DEM of their own and cannot source a crest. |

---

## Honest one-line summary

Of seven scenarios, **zero produce a valid DAM-BREAK run** — one where an impoundment
is emplaced, confined, and drained through a breach whose discharge is measured. That
is not a regression; it is what happens when the gates stop being tautologies. The
reasons are recorded per scenario above and per gate in each run's `validity` block.

**Amended 2026-09-14:** one scenario does produce a complete, artifact-backed,
mass-conserving run — `annamayya_stage2_wide` — as an explicitly-labelled
**forced-hydrograph inundation**, not a dam break. It is the right thing to
demonstrate on an Indian river, and it must be described as what it is. Calling it a
dam-break simulation would be the single easiest way to turn honest work into a
false claim.

## Where each deliverable stands, 2026-09-14

| # | Deliverable | Status |
|---|---|---|
| i | SPH **and** Delft3D, compared | **Half.** SPH is real and validated live against the Ritter analytical solution. Delft3D has never been run in this project. The second arm is a well-balanced 2D finite-volume SWE solver — same governing equations and numerical class — validated on the same benchmark. |
| ii | Customized tool, multiple input datasets | **Delivered.** 7 scenarios, custom DEM, custom population CSV, OSM auto-fetch with bbox-stamped caches. |
| iii | Dashboard GUI + .shp/.kml | **Delivered** (completed 2026-09-14, see above). |
| iv | Near-real-time via Google Earth Engine | **Not wired.** Module and tests exist; no caller, no credentialed account. |
| v | Indian river/dam, open-source data | **Delivered**, as a forced-hydrograph run on Annamayya. Four of the six *named* events still have no scenario. |
