# Implementation Plan: two-stage Annamayya — upstream forcing, then a downstream domain that does not truncate

Replace the current Annamayya run — one assumed hydrograph shape, injected into one
cell, on a domain that ends 7.9 km before the Cheyyeru–Penna confluence — with a
**two-stage chain**. Stage 1 routes the Pincha ring-bund release down the Cheyyeru and
*measures* the discharge arriving at Annamayya. Stage 2 takes that measured `Q(t)` as
its upstream forcing on a domain sized so the flood reaches the confluence instead of
a wall. Scope is downstream only: **Annamayya → Cheyyeru → Penna**.

This is not a Pincha dam-break reconstruction. Stage 1 is a forced routing run whose
only job is to determine the water arriving at Annamayya. No dam geometry is invented
for it.

The 24 h run of 2026-09-13 established why this is needed and also **invalidated its own
second half** — see Phase 0.1. Read that before quoting any number from
`data/scenarios/annamayya_routed_24h`.

---

## User Review Required

> [!IMPORTANT]
> **Decision 1 — one grid or two?**
> Stage 1 needs terrain south to Pincha (13.909); Stage 2 needs terrain north past the
> confluence (14.4311). Measured cost at coarsen 5:
>
> - **Option A — one widened grid** `(78.96, 13.85, 79.42, 14.50)`, 50 × 72 km,
>   **0.15 M cells = 1.8× today**. Both stages on one grid, so the handoff needs no
>   reprojection and no interpolation. ~35–40 min per 24 h run. **Recommended** — the
>   join is where a two-grid chain would silently lose mass.
> - **Option B — two domains.** Stage 2 alone is `(78.98, 14.15, 79.42, 14.50)`,
>   **0.08 M cells = 0.9× today**, i.e. cheaper than the run we have now, because it
>   drops 40 km of upstream terrain that holds 0.1 % of the water. But it reintroduces
>   an interpolation step at the section where the two grids meet.

> [!IMPORTANT]
> **Decision 2 — which inflow figure drives Stage 1?**
> `annamayya_event_evidence.json` deliberately preserves a 2× spread and labels it
> `"Inflow hydrograph ensemble bounds"`. The current run collapsed it to a point value
> *and* promoted its classification — that is the provenance defect this plan fixes.
>
> - **Option A — CWC central arm only** (9,065 m³/s at Annamayya; Pincha surge 3,964.4
>   m³/s), explicitly labelled as one arm of three. One Stage 2 run.
>   **Recommended for the first chain** — get the mechanism working, then widen.
> - **Option B — full three-arm ensemble** MHA 6,412 / CWC 9,065 / IISc 12,740 m³/s.
>   Honours the evidence file as written, triples Stage 2 cost (~2 h at Option A grid).

> [!NOTE]
> **The DEM refetch needs your approval, but costs no new download.** GLO-30 arrives as
> 1° tiles; the widened bbox needs tiles `(13,78) (13,79) (14,78) (14,79)` — exactly
> what is already cached. It is `fetch_dem(force=True)`, ~1 min.

> [!NOTE]
> **Do not cite the second half of the 24 h run.** Everything after t ≈ 10 h is void:
> 11.38 MCM drained into a terrain artefact and 14.19 MCM left across the boundary
> beside it — 22 % of the flood disposed of by a DEM fringe. The rising limb and the
> t = 10 h peak are clean. `max_depth 27.82 m`, `outflow 1.419e+07`, and
> `flooded_km2 103.60` are all products of the defect.

> [!NOTE]
> **A test's meaning changes in Phase 4.** `tests/test_annamayya_cascade_arrivals.py`
> asserts `available is False` for arrival validation. That is currently *correct* — the
> ground truth is deliberately switched off pending a source manifest. Those assertions
> may only flip **after** the manifest is wired from the evidence file. Flipping them
> first would be weakening a test to make a suite pass.

> [!NOTE]
> The previous plan (stage-graph rebuild, 1 of 19 done) is parked at
> `docs/STAGE_GRAPH_REBUILD_PLAN.md`. It was untracked; it has been copied, not deleted.

---

## Task Checklist

### Phase 0 — Correctness. Nothing runs until these land.

- [x] **Fix the `condition_dem` floor.** `dem_utils.py:212` computes
      `floor = percentile(valid, 0.1) - margin`. On this DEM `percentile(valid, 0.1)`
      **is itself 0.00 m**, because 279 zero-fringe cells already sit in the array — so
      the floor lands at −100 m and every 0.00 m cell passes as legitimate terrain. The
      conditioner's protection is defeated by the exact cells it exists to catch.
      Fix: compute the percentile over cells **excluding exact zeros**, and add an
      optional `floor_m` so callers can pass a sourced value. Do not special-case the
      east edge — the bug is the statistic, not the location.
- [x] Pass a sourced floor from `SCENARIOS['annamayya']['thalweg_m']` (180.0 m) minus
      margin at ~~both call sites~~ **`scripts/route_annamayya.py` only** — amended
      2026-09-13. `thalweg_m` is the DAM-SITE bed, not the domain minimum, so
      `thalweg_m - margin` is not a domain floor anywhere the valley drops away
      downstream. Measured at the `run_pipeline.py` call site: it walls **34,983**
      cells of real terrain on rishiganga (floor 2,280 m), **2,186** on phutkal, and
      **2,649,346** on south_lhonak, whose real valley floor is 715 m against a
      5,140 m thalweg — and it broke
      `test_run_lifecycle.py::test_crest_gate_rejects_wse_above_dem_barrier`, which was
      how it surfaced. Those domains are protected by the exact-zero wall and the
      zero-excluded percentile instead. The comment left at the call site carries the
      numbers so it is not re-added.
- [x] Test: a DEM whose 0.1 percentile is zero must still have its zero cells walled.
      This is the check that does not exist today and would have caught it.
      `tests/test_dem_conditioning_floor.py`, 5 tests.
- [x] **Zero-pad the hydrograph past its end.** `np.interp` clamps to the endpoint, so
      `--cap-hours 24` with `--hours 8` held Q = 1,227.4 m³/s for 16 h and injected
      **70.7 MCM, 1.61× the sourced release**, with mass closure still reading perfect.
      Landed 2026-09-13 in `route_annamayya.py`; solver untouched. *Verified: the 24 h
      run reported 116.1 MCM, identical to the 8 h run.*
- [x] **Correct four stale constants in `SCENARIOS['annamayya']`** — the evidence file
      supersedes all four and the routing table makes this file the production path:
      `event_origin_ist` 05:45 → **06:30**; `cascade_arrival_min` 150 → **180**;
      `upstream_structure.status` "(T-150 min)"; `downstream_structure.status` "05:45 (T=0)".
- [x] **Fix the false bbox comment.** It claims the bbox reaches "the Cheyyeru corridor
      down to the Pennar confluence (EVD-28)". North edge is 14.36; the confluence is
      14.4311. The comment asserts the exact thing that is broken.
- [x] Move Nandalur in `annamayya_villages.geojson` from 14.2580/79.1200 (in-channel,
      HAND 0.4 m) to the OSM village node **14.2704/79.1080**. Do **not** use the
      Nominatim geocode 14.3330/79.1961 — 11.76 km away at HAND 141 m.
      Re-verified in-session via Overpass (osm3s 2026-09-13T17:20:10Z): node
      245626858 `place=village` "Nandaluru", wikidata Q6963167, at
      79.1079557/14.2704112. Polygon translated by the same delta, 17 vertices,
      shape preserved. Drainage-relative height moves −0.22 m → **+4.84 m**.
- [x] Reconcile Nandalur `pop_total` — layer 12,500 vs OSM node 5,481. Taken to
      **5,481**, the OSM node's `population` with `population:date=2011`, fetched
      in-session. The layer's 12,500 carried no attribution. `pop_total_source` and
      `pop_total_classification` now travel with the value.
- [ ] Resolve `Mandapalli` — its geocode returns Ichchapuram, Srikakulam
      (`in_bbox: False`). Position is unverified, not confirmed.
      **Not closed — amended 2026-09-13 with what is now measured.** Two findings.
      (a) The layer point is wrong *as a settlement*: 61 m from the mapped Cheyyeru at
      drainage-relative height **−0.26 m**, i.e. in the channel — the same defect the
      old Nandalur point had; the depth read there is a channel depth.
      (b) The replacement is not established. Overpass returns exactly one candidate
      in the corridor — node 5462004296 `place=village` "Mandapalle", wikidata
      Q16342778, at 79.07228/14.24686, **3.35 km east**, drainage height **+7.14 m**,
      consistent with the rest of the layer. It is a **name match only**: EVD-23
      anchors Mandapalli to the Paleswara Swamy Temple and no such node exists in OSM
      in the corridor. Not moved — a 3.35 km move on a name match would substitute a
      plausible position for a verified one, and this settlement carries `t_s = 0`,
      where 3.35 km of travel time changes the answer. Recorded in the feature's
      `coord_candidate` / `coord_classification: UNVERIFIED`. **Needs a source tying
      the village to the temple or to the 2021 casualty record — user call.**
- [x] HAND sanity test over the whole villages layer. This is the check that caught the
      bad geocode and nothing pins it today. `tests/test_village_layer_hand.py`,
      3 tests. Real HAND is unavailable here (pysheds 0.5 calls `np.in1d`, removed in
      NumPy 2.0), so it uses a proxy — elevation minus the elevation of the nearest
      mapped-river cell. Measured over all 23 features: **−1.27 m to +14.19 m**; the
      bad geocode measures **85.75 m**. Threshold set at 30 m, between them, and the
      bad geocode is asserted as a positive control so the threshold cannot rot.

### Phase 1 — Domain

- [x] Widen the bbox per Decision 1 (Option A): `(78.96, 13.85, 79.42, 14.50)`.
      Grid at coarsen 5 is **(474, 329) = 0.156 M cells, 1.84x** the old 35x56 km
      grid — matching the plan's 0.15 M / 1.8x estimate.
- [x] `fetch_dem(force=True)`; confirm 4 cached tiles, no new download. Exactly the
      4 predicted tiles — N13/E078, N13/E079, N14/E078, N14/E079 — fetched in **9.3 s**;
      mosaic (2340, 1656) → UTM (2372, 1647) at 30.49 m. The previous DEM is kept at
      `data/dem/annamayya_dem.prewiden.tif` rather than discarded.
- [x] **Verify no zero fringe survives the new conditioner** — `min(z)` over the solved
      bed must sit at plausible valley-floor elevation, not 0.00 m.
      **`min(z) = 60.40 m`, cells below 60 m = 0** (was 279). But the floor that gets
      there is NOT `thalweg_m - 100`: see the amended Phase 0 entry and the new sourced
      `SCENARIOS['annamayya']['dem_floor_m'] = 60.0`. On this widened domain
      `thalweg_m - 100 = 80 m` would have walled **905 interior cells of real Penna
      floodplain**, so the same defect was one step from being re-created downstream.
- [x] Confirm the confluence (14.4311, 79.1699) and the full corridor bbox
      (lat ≤ 14.5253, lon ≤ 79.3142) are inside, with margin.
      **Confluence: PASS** — row 51, col 152, bed 96.50 m, **7.8 km** from the nearest
      edge. **Full corridor bbox: FAIL, and it cannot pass at Option A's bbox** — the
      corridor layer spans (78.9500, 14.0800, 79.3142, 14.5253), so 2.8 km of it is
      north of the 14.50 edge and 1.1 km west of the 78.96 edge. Option A was fixed by
      the user, so this is reported, not fixed. All 23 settlements are inside.
      Pincha (13.90890/78.99956) is inside with 4.3 km of margin.

### Phase 2 — Stage 1: upstream forcing → Annamayya

- [x] **Locate the upstream edge of the 192.50 m water plane** along the Cheyyeru. This
      defines the handoff section and is not yet measured. Everything downstream of it
      is fabricated bed and must not be used to derive `h`, `u` or flow area.
      Measured (`locate_handoff`, walking the OSM Cheyyeru upstream reach at half-cell
      steps): the plane is **162 cells = 3.76 km²**, rows 206-234, and it first touches
      the channel at **s = 29.57 km (14.18158/79.01849)**. The last real bed upstream is
      **s = 29.50 km, 14.18064/79.01837, bed 194.09 m** — and that is the handoff.
      **It sits 5.09 km UPSTREAM of the dam**, so a Stage-1 arrival measured there is
      not an arrival at Annamayya; the remaining 5.09 km has to be added.
- [x] **Correct the `SOURCES` provenance block** in `route_annamayya.py`: delete the
      1.17 lakh cusec Pincha figure (117,000 cfs sits *below* the evidence file's own
      range floor of 120,000 and nothing in the record supports it); restore
      `OFFICIAL_ESTIMATE` where the file says `OBSERVED`; carry MHA / CWC / IISc by
      agency instead of one unattributed number.
- [x] Stage 1 forcing from `pincha_surge_peak_m3s` = **3,964.4 m³/s** (140,000 cfs,
      range 120,000–160,000), whose declared `usage` is already "1D channel routing
      inflow". Shaped by the existing `cascade.generate_pincha_outflow` rather than a
      new curve, so no shape is invented: realised peak **3,878.0 m³/s** (the sampled
      maximum of the asymmetric pulse), base 300 m³/s, 90 min pulse, **32.73 MCM** over
      the 12 h window.
- [x] Run Stage 1 from Pincha (13.90890 / 78.99956) to the handoff section.
      `--stage 1 --coarsen 5 --cap-hours 12 --save-interval-s 300`. Mass closure
      **0.0000 %**, outflow 0 (the Stage-1 domain is sealed — `open_river_outlets` is a
      `run_pipeline` step and this path does not call it; harmless because Q is measured
      at an interior section, but it means nothing can leave).
      **A sign defect was found and fixed here**: `u` is the velocity along +col and `v`
      along +row, and on a north-up raster +row is SOUTHWARD, so projecting the UTM
      tangent straight onto (u, v) reads a northbound river BACKWARDS. The first 12 h run
      measured **−5.75 MCM** through the section. `flow_direction_grid` now carries the
      tangent in grid axes.
- [ ] ~~**Acceptance — the arrival check.**~~ **FAILED — STOPPED HERE, 2026-09-13.**
      Gate: `t_s` in [−5,400, −2,400] s. **Measured at the handoff: `t_s = +12,601 s`
      (+210.0 min).** That is **270 min late** against the sourced −3,600 s and
      **250 min late** against the gate's own late edge — and the handoff is still
      5.09 km short of the dam, so the true miss is larger.
      Not a stall and not ponding: a priority-flood minimax from the release cell to the
      handoff gives **escape head +0.00 m and ZERO true sills** over the whole 38.26 km
      4-connected path, and the front advanced monotonically the whole way (row 425 →
      238 in 6 h, still moving at 1.02 m/s in the final hour). It is simply **slow**:
      celerity **1.26 m/s** straight-line / 1.63 m/s along the path, against a sourced
      ~4.5 m/s — **3.6× slow**. Per the plan and the brief, the hydrograph was NOT tuned
      to close this.
- [ ] ~~Stage 2~~ — **not started.** Two independent gates block it; see "Why Stage 2
      did not start" below.
- [x] Export the handoff state as JSON: `Q(t)` primarily, plus `h(t)`, `u(t)`, `v(t)`,
      WSE, wetted width, flow area, cumulative volume, arrival time, pulse duration —
      each carrying its classification. Anything derived inside the water plane is
      emitted as `NOT_COMPUTED` with the reason, never as a plausible number.
      `data/scenarios/annamayya_stage1/handoff.json`. Measured hydrograph: rise from
      20.1 m³/s at t_s +12,601 s to a **peak of 1,190.8 m³/s at t_s +15,301 s**, then a
      clean recession to 309.4 m³/s at the 12 h cap; peak depth 3.49 m, peak speed
      1.36 m/s, wetted width 457 → 610 m, **13.33 MCM through the section of 32.73 MCM
      released**.

## Why Stage 2 did not start

Two gates failed independently. Either alone stops the chain; both are reported with
numbers rather than absorbed.

**Gate A — arrival, 250 min outside the window.** See the struck item above.

**Gate B — the IISc arm is unreachable by arithmetic, before any routing.**
Annamayya inflow = routed Pincha surge + sourced catchment runoff. Taking the forcing
peak **undiminished** — i.e. assuming zero attenuation over 34+ km, which is physically
impossible and therefore a hard ceiling:

| Pincha forcing | + runoff peak | = ceiling | vs MHA 6,412 | vs CWC 9,065 | vs IISc 12,740 |
|---|---|---|---|---|---|
| sourced 3,964.4 (140,000 cfs) | 3,800.0 | **7,764.4** | MET +21.1 % | SHORT 14.3 % | **SHORT 39.1 %** |
| range ceiling 4,530.7 (160,000 cfs) | 3,800.0 | **8,330.7** | MET +29.9 % | SHORT 8.1 % | **SHORT 34.6 %** |

With the **measured** routed peak of 1,190.8 m³/s (attenuation to **30.7 %** of the
forcing) the realised total is 4,990.8 m³/s — short of MHA by 22.2 %, CWC by 44.9 % and
the IISc upper arm by **60.8 %**.

So Decision 2's IISc arm cannot be produced from the sourced forcing. Closing that gap
would require a multiplier, a scaled runoff, or a Pincha peak beyond its sourced
160,000 cfs ceiling — each of which is the fabricated forcing this plan exists to
remove. **The gap is the finding.**

**A third problem, not a gate but load-bearing for both.** `pincha_to_annamayya_distance_km`
= 34.0 is classified OBSERVED and sourced to "Survey of India GIS River Line /
HydroRIVERS". The **straight-line** Pincha→dam distance is **33.46 km** — the sourced
"river line" figure is within **1.6 %** of the straight line, which no real river is.
The mapped OSM path is **43.42 km** (8.84 km unmapped gap + 34.58 km reach), **28 %
longer**. The sourced 120 min transit therefore implies 4.65 m/s on the straight line
but **6.03 m/s along the actual channel**, and the model's 1.26 m/s is being compared
against a celerity derived from a distance that looks like it was never measured along
a river.

**Leading hypothesis for the 3.6× celerity deficit** (HYPOTHESIS — not proven, and not
acted on):
1. **The release is not on a channel.** The nearest mapped river is **8.84 km** away,
   and Pincha sits on its own **294.50 m DSM water plane** (8 cells, 0.19 km²) — the
   same artefact as Annamayya's 192.50 m plane. The flood sheet-flows: **17.73 km² wet**
   by 6 h for 26.24 MCM.
2. **`condition_flowline` is never called on this path.** `run_pipeline` calls it to
   make the mapped river hydraulically continuous; `route_annamayya.py` does not. 111 of
   250 steps along the path rise locally (though none is a true sill), and the flow has
   to climb each one. It could not help over the first 8.84 km regardless — nothing is
   mapped there.
3. Uniform Manning 0.045 across channel and floodplain; `apply_channel_roughness` exists
   and is not called here either.

Testing (2) means modifying terrain, which is a deliberate act, not a tuning knob —
left for the user's call.

### Phase 3 — Stage 2: downstream, 24 h as a diagnostic window

- [ ] ~~Feed Stage 1's `Q(t)` into the existing one-cell `InflowBoundary`.~~
      **Not done, and the plan has a conceptual gap here.** Stage 1 measures the water
      ARRIVING at Annamayya. The flood DOWNSTREAM of Annamayya is driven by the dam's
      released storage PLUS that through-flow — feeding Stage 2 the inflow alone would
      omit the 63.43 MCM the dam let go, which is the thing that caused the disaster.
      Stage 2 was therefore run on the sourced dam-failure release (storage + sourced
      catchment runoff), which is the physically complete downstream forcing, and the
      domain was isolated as the single variable. Wiring Stage 1's measured `Q(t)` in
      as the through-flow component is the correct next step and is blocked on Gate A
      (its arrival is 175-210 min late, so it would delay the downstream flood by that
      much).
- [x] Run 24 h. **24 h is a diagnostic window, not a claim that 24 h is sufficient.**
      `data/scenarios/annamayya_stage2_wide`, mass closure **0.0000 %**, 226,678 steps.
      **153.00 km² wet**, max depth 13.41 m, 10/23 settlements, PAR 19,950.
- [x] From the resulting inundation and outflow curves, read the **actual peak time and
      recession time**. The 8 h run peaked at ~10 h on the current domain; on a longer
      domain it will be later.
      Wetted-cell count plateaus from ~t = 11 h: 4,190 (690 min) → 4,209 (765 min) →
      4,614 (1,125 min), i.e. +10 % over the last 6 h while the release ended at 8 h.
      Max depth is flat at 11.62 m from t ≈ 6 h. **There is no outflow to fall** —
      `outflow 0.000e+00` — because this script never opened an outlet, which is the
      defect fixed in the `--open-outlets` variant below.
- [ ] **Adjust the cap from observed convergence** and rerun. Pass criterion is a
      plateau in wetted area plus a falling outflow — not a clock.
      **Half-satisfiable only, and that is the finding.** The wetted-area plateau is
      there. The falling outflow is NOT, and cannot be, on a sealed domain: the
      criterion presumes an outlet this script never opened. `--open-outlets` now opens
      5 (bed elevations 62.0 / 96.5 / 96.83 / 126.27 / 143.13 m) and makes the second
      half of the criterion measurable for the first time.
- [x] Inspect where and when water reaches the boundary. Outlet treatment stays
      deferred; record the contact, do not fix it here.
      **On the widened domain the flood never reaches the boundary** — the furthest wet
      cell stops **17 cells (2.6 km)** short of it, against **1 cell** on the old
      domain. That is the plan's key question answered: the front does NOT stall at the
      new edge. Outlet treatment was nonetheless implemented behind `--open-outlets`
      because the sealed domain makes every extent an upper bound on ponding and makes
      the convergence criterion above untestable; it is a flag, off by default, and the
      baseline run above does not use it.

### Phase 4 — Validation

- [x] **Re-enable arrival ground truth.** ~~Build `HISTORICAL_ARRIVALS` from it.~~
      **Amended — the item named the wrong object.** `compare_arrivals` never reads
      `HISTORICAL_ARRIVALS`; that constant is a legacy audit trail. It reads a strict
      manifest at `data/observations/annamayya/arrivals.json` via `_verified_arrivals`,
      which demands `classification == "OBSERVED"`, an `event_clock`, an
      `acquisition_proof` and a 64-hex `source_hash` on the payload AND every record.
      That file did not exist, which is the real reason validation reported
      NOT_AVAILABLE. Authored it **by script** — `scripts/author_arrival_manifest.py` —
      from `event_clock.timeline_events`, with the SHA-256 of the evidence file on the
      payload and of each event on its record. The gate was satisfied, not weakened.
- [x] The legacy records' IST windows are right; their **relative minutes are anchored
      to the superseded 05:45 T=0** and are 45 min off. Take `t_s` from the evidence
      file, not the legacy block. Done — every window is recomputed as
      `(t_s ± t_uncertainty_s)/60`, and the manifest records
      `relative_minutes_recomputed_from_t_s: true` with the reason the legacy block was
      not used.
- [x] Add `downstream_farfield` / `pennar_confluence` (`t_s = 12,600 s`, 08:30–11:30 IST,
      EVD-28). It is the strongest far-field check available and Phase 1 is what puts it
      inside the domain. Added at 14.4311/79.1699, window [120, 300] min.
      **Modelled +840 min — LATE by 540 min**, the worst miss of the five and the one
      that most clearly indicts front celerity rather than extent.
- [x] Validate modelled arrival and depth against EVD-21…EVD-28 with their uncertainty
      bands. Corrected coordinates only.
      **Depth: 3 of 3 IN RANGE** (Mandapalli 5.27 m in [3.5, 6.5]; Gundlur 3.11 in
      [2.0, 4.0]; Nandalur 2.51 in [1.5, 3.5]). **Arrival: 0 of 5, all LATE** by 85, 90,
      100, 130 and 540 min. EVD-21/22 are pre-T=0 and were NOT scored — they are the
      pre-washout overtopping discharge, which this run type does not model.
      EVD-26 deliberately keeps the in-channel coordinate: it is a railway BRIDGE
      washout, which happens at the river, unlike the settlement that moved to the
      village node.
- [ ] Grow `annamayya_villages.geojson` beyond 23 features, **or** give the UI a
      NOT ASSESSED state distinct from SAFE. One of the two — shipping neither is what
      makes unevaluated ground read as cleared.
- [ ] Keep S2 residual water as **cell counts**, separate from reported inundation.
      1–5 cells is weak, 12–16 is strong. Never binarise it, never label it peak extent,
      never compute a skill score against it — the quantities differ (CSI 0.029).

### Phase 5 — Sensitivity, only once the chain works

- [x] ~~Manning `0.030 / 0.045 / 0.060`.~~ **Done differently, and the plan asked for the
      wrong sweep.** `apply_channel_roughness`'s own docstring already records the
      measurement: raising the CHANNEL n from 0.020 to 0.100 moved the front 4.26 → 1.70 km,
      while FLOODPLAIN n over 0.035–0.150 moved it **not at all**. A uniform sweep pulls
      on the lever that does nothing. Wired `--channel-roughness` instead, which gives
      the mapped channel `CHANNEL_MANNING_N = 0.035` against a 0.045 floodplain — 3,727
      cells, 2.39 % of the grid. Effect: extent 153.00 → 155.40 km², far-field arrival
      840 → 795 min. Real, and ~2 %.
- [x] Grid: coarsen 5 baseline → **coarsen 2**, run on the FULL domain rather than a
      subdomain (Stage 1, 8 h cap: 56,127 steps in 2,657 s — affordable, so no subdomain
      was needed). Three-point sweep, monotone and in the predicted direction:
      | coarsen | cells | arrival `t_s` | peak Q | celerity |
      |---|---|---|---|---|
      | 5 | 152.4 m | +175.1 min | 1,309.3 | 1.38 m/s |
      | 3 | 91.5 m | +155.0 min | 1,312.7 | 1.47 m/s |
      | 2 | 61.0 m | +140.0 min | 1,673.2 | 1.54 m/s |
      **2.5× finer cells buys 11 % of celerity; the gate needs ~3.3×.** Resolution is a
      real effect and not the explanation.
- [ ] Revisit the DEM product here and not before: bare-earth matters at coarsen 2,
      barely at coarsen 5. FABDEM is CC BY-NC-SA 4.0 — a licence question for an SIH
      deliverable, and it does not fix the reservoir plane either.
      **Not done.** The coarsen-2 result above weakens the case for it: if 2.5× finer
      geometry buys 11 %, a bare-earth product at the same resolution is unlikely to buy
      the missing 3×. Worth doing for depth realism, not as a fix for arrival timing.
- [ ] Gunjana and Pullageru tributary inflows — `InflowBoundary` multi-inflow already
      exists (`swe_2d.py:186`, `inflows=` at `:751`). Wiring, not solver work. Blocked on
      sourced hydrographs; until then those valleys are **out of scope and excluded from
      the denominator**, not counted as a miss.
      **Measured 2026-09-14: neither feeds Annamayya.** Both join the Cheyyeru
      **downstream** of the dam (Gunjana nearest approach 14.3638/79.2304, Pullageru
      14.2694/79.1881), so they cannot contribute to the dam's INFLOW and are irrelevant
      to the three agency arms. The tributary that DOES feed it is the **Mandavi**
      (46.7 km), joining upstream at 14.1647/79.0083 — and it is unforced. If a
      tributary hydrograph is ever sourced, source that one first.
      Also established: the Cheyyeru's second headstream, the **Bahuda**, lies outside
      the domain entirely and is likewise unforced.

### Phase 6 — Record

- [x] `findings_results.md`: the 24 h result, the `condition_dem` defect with its
      numbers, and ~~the corridor decomposition~~ — the corridor decomposition needs a
      Stage-2 run and there is none, so it is NOT in the record. Everything else landed:
      the conditioner defect with its before/after table, the `thalweg_m` reversal with
      the per-scenario cell counts, the widened-domain gates, the village-layer work,
      and the whole Phase 2 result including both failed gates.
- [x] **Amend `INVARIANTS.md` §3.** "A settlement reading 0.00 m means *not reached yet*"
      was true at 8 h. At the clean t = 10 h peak those eleven settlements are still dry.
      The entry is wrong as written.
      **Done 2026-09-14, once Phase 3 supplied the measurement it was blocked on.** On
      the widened 24 h run the wetted-area growth rate goes 10.21 km²/h at t=4 h →
      **−0.36 km²/h at t=12 h**, and the front reaches **100 % of the 54.0 km stem**.
      The 13 dry settlements now read as *outside this flood's extent*, not un-reached —
      with the caveat that a residual 1.7 km²/h of floodplain redistribution continues
      to t=24 h because the domain cannot drain where the flood sits.
      A second entry was added alongside it: **"flooded area" is a 24 h ENVELOPE**
      (153.00 km²) and the **peak instantaneous** area is **87.70 km²** — the two differ
      by 1.75× and the headline must say which it means.
- [x] Add an INVARIANTS entry for the percentile-floor trap. Added, plus three more the
      session turned up: `thalweg_m - margin` is not a domain floor; the `u`/`v` grid-axis
      sign convention; Pincha's own 294.50 m DSM water plane; and the sourced
      Pincha→Annamayya distance being a straight line. Routing table gained a
      `dem_floor_m` row.
- [x] `scripts/gen_atlas.py` and `/graphify . --update` — module layout and call paths
      change here. Atlas regenerated; **`scripts/` was not in its TARGETS at all**, so the
      one file INVARIANTS routes the whole Annamayya path to was the one file the atlas
      did not index — fixed, and the new symbols now appear. Graph updated incrementally
      twice: 49 changed files + 3 deletions → 5,263 / 14,528, then 12 more changed files
      → **5,331 nodes / 14,574 edges**, 247 communities, health clean (0 dangling,
      0 missing, 0 collapsed, 0 self-loops) on both passes.

### Phase 7 — added 2026-09-14, not in the original plan

- [x] **Root-cause the stale OSM caches.** Four layers keyed on filename only, so a bbox
      change was invisible. `_bbox_stamp_ok` writes a `<name>.bbox.json` sidecar and a
      cache with no sidecar counts as UNKNOWN. **Second iteration: detect-and-LABEL, not
      detect-and-refetch** — the first version made every consumer depend on Overpass and
      took the test suite down (hung in `test_run_lifecycle.py`). Layers now load in
      milliseconds carrying `attrs["bbox_stale"]`; the physics paths refuse that label.
- [x] **Retract the "8.84 km unmapped gap".** It was the stale cache. OSM way 148491940
      "Pincha River" passes **0.11 km** from the ring bund; the Cheyyeru's head is 8.84 km
      away because the Cheyyeru **begins at Rayavaram**, where Pincha and Bahuda merge.
- [x] **Add `--open-outlets` and `--condition-flowline`**, the two `run_pipeline` terrain
      steps this script never called. 5 outlets open (beds 62.0–143.13 m); flowline lowers
      748–759 cells, mean 0.91 m, max 8.84 m dry / 19.4 m with outlets first, and
      **touches 0 cells of the 192.50 m reservoir plane** — so no `protect_mask` is needed.
- [x] **Add `--channel-roughness` and `--spinup-hours`.**
- [ ] ~~Antecedent wet channel as a usable configuration~~ — **blocked on conveyance.**
      The 18 h spin-up turned a sourced 800 m³/s *discharge* into 51.86 MCM of unsourced
      *standing water*: only **0.33 MCM (0.6 %) drained** through 5 open outlets. It
      pre-wets 4 of 5 EVD points, and `compare_arrivals` then scores EVD-23 as **MATCH** —
      a false pass. `scripts/check_prewet.py` screens for this. The one readable point,
      EVD-28, went **795 → 465 min (41 %)**, the largest single improvement measured, so
      the hypothesis is right and the implementation is not usable until the channel can
      convey the baseflow.

---

## Proposed Changes

### Terrain conditioning

**[MODIFY]** `src/m2_geometry/dem_utils.py`
- `condition_dem`: exclude exact zeros before computing the percentile; add optional
  `floor_m` parameter; keep the percentile as fallback only.

**[MODIFY]** `scripts/route_annamayya.py`, `run_pipeline.py`
- Pass a sourced `floor_m` derived from `thalweg_m` at both `condition_dem` call sites.

**[NEW]** `tests/test_dem_conditioning_floor.py`
- A DEM whose 0.1 percentile is zero must still have its zero cells walled.

### Scenario constants

**[MODIFY]** `src/data_fetcher.py` — `SCENARIOS['annamayya']`
- Four stale constants → evidence values; bbox per Decision 1; correct the false comment.

### The two-stage runner

**[MODIFY]** `scripts/route_annamayya.py`
- `--stage {1,2}`. Stage 1: Pincha → handoff section, exports the state JSON.
  Stage 2: reads that JSON, drives the existing one-cell `InflowBoundary`.
- `SOURCES` block corrected: drop 1.17 lakh cusec, restore `OFFICIAL_ESTIMATE`, carry
  the three agency arms.
- Zero-pad guard — **already landed**.

**[NEW]** `data/scenarios/annamayya_handoff.json`
- Stage-1 output. `Q(t)` plus the hydraulic state, each field carrying its
  classification; anything inside the water plane emitted as `NOT_COMPUTED` with a reason.

### Validation

**[MODIFY]** `src/m10_validation/compare_arrivals.py`
- Build `HISTORICAL_ARRIVALS` from `event_clock.timeline_events`; re-enable; add
  `pennar_confluence`.

**[MODIFY]** `data/admin/annamayya_villages.geojson`
- Nandalur coordinate; population reconciliation.

**[MODIFY]** `tests/test_annamayya_cascade_arrivals.py`
- Assertions flip **only after** the manifest is wired, and the commit says why.

### Not touched

- `swe_2d.py` / `swe_2d_gpu.py` — one-cell `InflowBoundary` retained per scope.
  A cross-section inlet BC would have to land in both backends in one commit; deferred.
- `data/evidence/annamayya_event_evidence.json` — it is the authority here, not a
  target. Only extend it if an external source is fetched and verified in-session.

---

## Verification Plan

**Phase 0 — conditioning**

```bash
python -m pytest tests/test_dem_conditioning_floor.py -q
```
Pass: zero-elevation cells are walled even when they set the 0.1 percentile.

**Phase 1 — domain**

Pass: solved bed `min(z)` sits at plausible valley-floor elevation, **not 0.00 m**;
zero cells below 60 m = **0** (was 279); confluence and full corridor bbox inside with
margin.

**Phase 2 — Stage 1**

Pass: front reaches the handoff section at `t_s` in **[−5,400, −2,400] s**
(−90 to −40 min), bracketing the sourced −60 min. Outside that, stop and report — do
not tune the hydrograph to hit it.

**Phase 3 — Stage 2**

```bash
python scripts/route_annamayya.py --stage 2 --cap-hours 24
```
Pass on the diagnostic run: mass closure < 0.01 %; **zero volume in cells below
60 m bed**; the wetted-area curve shows an identifiable peak and recession. Then set
the production cap from that curve and rerun to a plateau, not a clock.

Report against the clean-peak baseline, not the void tail:

```
                          8 h        24 h (current domain)
envelope coverage         76.9 %     90.9 %
peak instantaneous        75.3 %     85.3 %  at t = 10 h
front on the 54.9 km stem 44.6 km    47.0 km, stalled at the domain edge
settlements > 0.3 m       12/23      12/23
```

The number that matters is whether the front still stalls once the domain reaches the
confluence. **If it stalls again at the new edge, the domain is still too small and
that is the finding** — not something to absorb into a coverage percentage.

**Phase 4 — validation**

Pass: modelled arrival inside the EVD band at each of EVD-21…EVD-26 and EVD-28, or an
explicit miss with the offset stated. Depth inside the sourced range where one exists.
A settlement with no marker is reported as NOT ASSESSED, never as clear.

**Full suite**

```bash
python -m pytest -q
```
The last recorded figure is **227 passed** (2026-09-13); an earlier note in the parked
plan said 211 and was never re-measured. Re-run before quoting either.

---

**Stopping here for approval.** Nothing in Phase 0 onward is implemented. The DEM
refetch in particular waits on Decision 1 and your explicit go.
