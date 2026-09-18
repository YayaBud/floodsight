# Implementation Plan: rebuild FloodSight around event cascades, not reservoirs

The four Indian events are **not** "a reservoir with a crest that breaks". Each is a
*cascade* with a formation stage, and for two of them the impoundment either did not
exist before the flood or was created by the event itself. The current architecture
models one object — a full pool behind a typed crest — and every scenario fails
because it is being forced into a shape it does not have.

This plan replaces that model. It does not patch it.

---

---

## Blocking prerequisites — added 2026-09-13, do these first

The stage-graph rebuild below is **unstarted (1 of 19 done)** and is not replaced by
this. But four defects found on 2026-09-13 make any Annamayya result — old
architecture or new — untrustworthy, and three of them are cheap. Clear these before
spending compute on anything Annamayya-shaped.

> [!IMPORTANT]
> **Decision needed before the long rerun.** The 24 h rerun costs ~8x the 144 s run
> (coarsen 3, 24 h ≈ 20–40 min wall). Two ways to spend it:
>
> - **Option A — fix inputs first, then one long run.** Correct the Nandalur
>   coordinate, grow the village layer, extend the bbox north past 14.45, open the
>   boundary, then run once. Recommended: a run launched before these reproduces the
>   same wrong answer more expensively.
> - **Option B — rerun now at 24 h on the current domain.** Cheaper to start, but the
>   domain still ends ~8 km short of the Penna confluence and outflow is still 0, so
>   the extent stays a ponding upper bound and Nandalur stays 11.7 km misplaced.

> [!NOTE]
> There is no observed extent to validate against — no satellite saw this flood
> (12-day Sentinel-1 gap over the reach; Sentinel-2 4 h after the breach into 98.7 %
> cloud). Do not plan a CSI-based acceptance test for Annamayya. The only external
> check available is the +5 d residual-water layer, which is directional evidence
> only. See `INVARIANTS.md` §3 and `findings_results.md` 2026-09-13 entries.

### Prerequisite checklist

- [ ] Move Nandalur in `data/admin/annamayya_villages.geojson` from 14.2580/79.1200
      (in-channel, HAND 0.4 m) to the OSM village node **14.2704/79.1080** (HAND 3.7 m).
      The run gives 2.55 m at the current point and **0.24 m at the town** — this
      changes the EVACUATE rank #1 call. Do NOT use the Nominatim geocode
      14.3330/79.1961: it is 11.76 km away at HAND 141 m (see the correction entry).
- [ ] Reconcile Nandalur `pop_total` — layer says 12 500, OSM node says 5 481.
- [ ] Add a HAND sanity test over the whole villages layer (a settlement on a flooded
      river cannot sit tens of metres above nearest drainage). This is the check that
      caught the bad geocode and nothing pins it today.
- [ ] Resolve `Mandapalli` — its geocode returns Ichchapuram, Srikakulam
      (`in_bbox: False`), so its position is unverified rather than confirmed.
- [ ] Grow `annamayya_villages.geojson` beyond 23 features to cover the domain, **or**
      give the UI a NOT ASSESSED state distinct from SAFE (`ui.md`). One of the two —
      shipping neither is what makes unevaluated ground read as cleared.
- [ ] Extend the Annamayya bbox north past **14.45** so the Cheyyeru–Penna confluence
      (14.4311, 79.1699) is inside the domain; refetch the DEM over the new bbox.
- [ ] Open the north/east boundary so `outflow` can be non-zero. Today it is
      `0.000e+00 m3` and the extent is an upper bound on ponding.
- [ ] Re-run: `python scripts/route_annamayya.py --coarsen 3 --hours 24 --cap-hours 24`
- [ ] Confirm the rerun ended on **quiescence, not the cap** — wetted-area growth
      flattened, and `outflow > 0`. If it hit the cap again the result is still
      truncated and must be reported as such.

### Note on the baseline below

`Verification Plan` cites "current baseline: **211 passed**". The suite reported
**227 passed** on 2026-09-13 after the forced-hydrograph validity work. The 211 figure
was not re-measured this session — re-run before quoting either number.

---

## The measurement that settles it

Question asked: *how much does our Phutkal dam actually hold?*

**Corrected 2026-09-13.** An earlier version of this section reported that the pool
spilled off-grid above 3762.5 m and that no lake was possible. That was an artefact of
running the fill on an **unconditioned** DEM, whose nodata cells sit at `z = 0.0` and
connect the whole grid. With `condition_dem` applied the curve is monotone and
physical:

| crest m | height above thalweg m | V MCM |
|---|---|---|
| 3745 | 8.9 | 0.11 |
| 3780 | 43.9 | 10.32 |
| 3795 | 58.9 | 19.44 |
| 3805 | 68.9 | 27.05 |
| **3810** | **73.9** | **31.38** |
| 3825 | 88.9 | 46.44 |

Thalweg at the blockage: **3736.11 m**.

**The volume observable inverts cleanly.** The documented deposit was **69 m** high and
impounded **>30 MCM** (Cartosat-2, *Landslides* 14:529-538). A 69 m deposit gives crest
3805 m and 27.05 MCM; 30 MCM arrives at 3810 m, a 73.9 m deposit — agreement within
~5 m of deposit height.

This also explains the repo's existing numbers exactly. Typed `dam_height_m = 58.0`
gives 3736.11 + 58 = **3794.11 m**, which is precisely the manifest's `water_level_m`,
and yields ~18.98 MCM — the figure already quoted in `run_pipeline.py`'s G4 comment.
**The defect is one wrong constant: the deposit was 69 m, not 58 m.**

What is **not** yet established:

1. **The length observable.** Documented lake length is 17.2 km. Two attempts to
   measure it were both wrong metrics (the river extract's own extent; then a
   straight-line distance transform). A true in-pool geodesic is written but has not
   run against a verified-bounded pool. **The crest above therefore rests on the volume
   observable alone.**
2. **Domain extent.** Upstream of the blockage is **WEST** — measured by thalweg walk,
   +20.5 m over 4.64 km west against -17.7 m over 4.96 km southeast. Whether a 17.2 km
   lake fits inside the AOI's western arm is untested.

---

## What each event actually is

| event | real sequence | what the repo models |
|---|---|---|
| **Phutkal 2015** | rockfall 31 Dec 2014 to a 69 m natural dam, then **128 days of snowmelt filling**, then overtopping breach 7 May 2015 08:10 | full pool appears at t=0, breaks |
| **Rishi Ganga 2021** | 22 Mm3 rock-ice wedge falls ~2 km, frictional melting **fluidises** it, debris torrent destroys two HEPs. **The lake formed AFTER the flood.** | a 2175.68 m reservoir that never existed |
| **South Lhonak 2023** | 14.7 Mm3 moraine collapse into the lake at 22:12:20 IST, **20 m impulse wave**, moraine overtopped and breached, 50 Mm3 released, 60 km of routing entraining **270 Mm3** of sediment, Teesta III (60 m) destroyed 00:30 | one breach, no wave, no second structure |
| **Annamayya 2021** | cloudburst, Pincha inflow 1.17 lakh cusec, **Pincha ring bund fails ~03:30**, routed into Annamayya, inflow >2 lakh against 2.17 lakh capacity and peaking at 3.2 lakh, earthen section washes out ~05:30, 10 villages | a single structure with typed cascade constants |

**On Rishi Ganga specifically** — the "basin level" language in the current gates is an
artefact of forcing a non-impoundment event into an impoundment model. There was no
lake. `water_level_m = 2175.68` is manufactured by the manifest generator, and G4's
complaint that it sits above the escape level is the model objecting to a reservoir
nobody ever built. The correct answer is not to move the level. It is to delete the
reservoir from this event entirely.

---

## The architecture

**An event is a directed graph of stages.** A node is a physical process with its own
solver and its own provenance. An edge is a handoff of a physical quantity — a
hydrograph, a volume, a wave height, a sediment load.

Six process types. The repo has one and a half of them.

| process | computes | needed by | status today |
|---|---|---|---|
| `TRIGGER` | release volume and timing of the initiating mass or rainfall | all four | **absent** |
| `FORM` | deposit geometry burned into the DEM, giving a stage-storage curve and the lake | phutkal, south_lhonak | **absent** (lake is a typed number) |
| `FILL` | inflow integrated against stage-storage, giving level vs time and the overtopping date | phutkal (128 d), annamayya (hours) | **absent** |
| `WAVE` | landslide into a lake, giving impulse wave height and overtopping volume | south_lhonak | **absent** |
| `FAIL` | erosional breach growth, giving an outflow hydrograph | phutkal, south_lhonak, annamayya x2 | **parametric only** (typed peak Q) |
| `ROUTE` | 2D depth-averaged routing | all four | **clear water only** |

Per-event graphs:

    phutkal        TRIGGER -> FORM -> FILL(128 d) -> FAIL(overtop erosion) -> ROUTE
    rishiganga     TRIGGER -> ROUTE(bulked/two-phase) -> IMPACT x2    [no FORM, no FILL, no FAIL]
    south_lhonak   TRIGGER -> WAVE -> FAIL(moraine) -> ROUTE(+entrainment) -> FAIL(Teesta III) -> ROUTE
    annamayya      TRIGGER(rain) -> FILL(Pincha) -> FAIL(ring bund) -> ROUTE -> FILL(Annamayya) -> FAIL -> ROUTE

### The idea that replaces typed constants: invert the lake

The lake is not an input. It is what the DEM gives you once a deposit is placed. The
historical record supplies **two** independent observables — impounded volume **and**
lake length — against **one** unknown, the deposit crest. So:

> Fit the crest so the DEM basin reproduces both V and L. If the level that gives
> 30 MCM is not the level that gives 17.2 km, the deposit footprint or the DEM is
> wrong — and that disagreement is the finding, not something to average away.

This is how `crest_elev_m` gets sourced without a survey, and it is checkable. The same
inversion applies to South Lhonak (50 Mm3 released, lake area known from imagery).

## User Review Required

> [!IMPORTANT]
> How much of this do you want built?
>
> - **Option A — the lake-formation spine (recommended).** `TRIGGER -> FORM -> FILL ->
>   FAIL -> ROUTE` built properly for **Phutkal only**, end to end, animated from the
>   31 Dec 2014 rockfall through 128 days of filling to the 7 May breach. It is the
>   deliverable the problem statement actually asks for, and Phutkal is the one event
>   where every stage is documented and clear-water physics is defensible.
> - **Option B — spine plus cascade.** A, plus multi-structure cascade so Annamayya
>   runs Pincha into Annamayya as two real failures with routing between them.
> - **Option C — everything, including the debris phase.** B, plus a bulked/two-phase
>   `ROUTE` so Rishi Ganga and South Lhonak stop being refused. This is the largest
>   piece by far: entrainment, an evolving bed, and a second momentum equation.

> [!NOTE]
> Two constraints worth knowing before choosing.
>
> **Domain size.** Phutkal needs at least 17.2 km of lake plus roughly 100 km of
> routing to Padum. The current AOI cannot hold the lake, let alone the reach. Either
> the domain grows (and the 2D solver gets slow) or routing goes 1D on the long reach
> and 2D only where consequences are computed. This is a real architectural fork and
> should be decided before any solver work.
>
> **Rishi Ganga cannot be made valid under Option A or B.** It has no impoundment to
> form and no dam to break. Under those options the honest outcome is to reclassify it
> as a debris-flow event that is out of model, not to keep it failing gates that do not
> apply to it.

## Task Checklist

### Phase 1 — Event graph and the formation stage
- [ ] Define the stage-graph schema: node = (process, params, provenance), edge = a
      typed physical handoff. One JSON per event, replacing the flat `SCENARIOS` dict
- [ ] `FORM`: deposit geometry to burned DEM to stage-storage curve to lake polygon
      **— must span the valley cross-section, key into terrain above the water line,
      and VERIFY CLOSURE (no upstream->downstream path). A disc and a
      below-crest-band were both tried and both leak. Closure verification is the
      operator's core, not a detail.**
- [x] **Crest inversion (volume half)**: 30 MCM arrives at crest 3810 m = a 73.9 m
      deposit, against a documented 69 m deposit -> agreement within ~5 m. The typed
      `dam_height_m = 58.0` is simply wrong. *Length half still unproven.*
- [ ] Re-derive Phutkal's deposit from the Cartosat-2 record (69 m high, 600 m long,
      90,422 m2 debris area) rather than a ridge-search rectangle
- [ ] Size the domain from the inverted lake extent, not a typed bbox.
      **Upstream is WEST (+20.5 m / 4.64 km) not SE (-17.7 m / 4.96 km).**
      `data/dem/phutkal_wide_dem.tif` was fetched eastward and is useless — refetch west.

### Phase 2 — Filling, and the timeline that makes it visible
- [ ] `FILL`: degree-day snowmelt plus baseflow, integrated against the stage-storage
      curve
- [ ] Output a **level-vs-time curve** from 31 Dec 2014 to overtopping; the predicted
      overtopping date is the acceptance test and must land near 7 May 2015
- [ ] Animate the filling stage: lake polygons through the 128 days, then the breach.
      The animation currently starts at the breach; formation is the deliverable

### Phase 3 — Erosional breach and the ensemble
- [ ] `FAIL` as an erosion model producing a hydrograph, not a typed peak Q
- [ ] Landslide-dam breach parameters from the **landslide-dam** literature
      (Peng and Zhang 2012), not embankment regressions (Froehlich) — different
      material, different dataset
- [ ] Ensemble over the genuinely uncertain parameters — erodibility, final breach
      width, inflow year — reported as P5/P50/P95 envelopes. This is the
      best-case/worst-case ask, done as a distribution rather than three typed arms

### Phase 4 — Cascade (Option B and up)
- [ ] Stage N's outflow hydrograph becomes stage N+1's inflow boundary. The current
      "injection and opening are mutually exclusive" rule was the right fix for a
      double-count bug and is the wrong rule for a cascade; it needs replacing with a
      provenance-tracked handoff, not simply relaxing
- [ ] Annamayya: Pincha ring-bund failure, Cheyyeru routing, Annamayya filling,
      earthen-section washout — four stages with real numbers

### Phase 5 — Debris phase (Option C only)
- [ ] Bulked or two-phase `ROUTE` with entrainment
- [ ] Rishi Ganga: fluidisation of a 22 Mm3 rock-ice wedge, no impoundment anywhere
- [ ] South Lhonak: `WAVE` (impulse wave from a 14.7 Mm3 collapse), moraine breach,
      routing that entrains 270 Mm3, Teesta III as a second structural failure

### Phase 6 — Verification
- [ ] Phutkal: predicted overtopping date against 7 May 2015; inverted lake V and L
      against 30 MCM and 17.2 km; both reported as numbers, not adjectives
- [ ] Full suite green (current baseline: **211 passed**, 579 s)
- [ ] `scripts/gen_atlas.py` plus `/graphify . --update` — the graph is from
      2026-09-12 and this changes module layout substantially

## Verification Plan

Acceptance is **reproducing the event's own observables**, not passing a gate:

- Phutkal lake at the inverted crest: **at least 30 MCM and 17.2 +/- 2 km**, impounded,
  not touching the domain edge.
- Filling curve overtops within a **+/- 14 day** window of 7 May 2015.
- Breach hydrograph peak within the ensemble's P5-P95 band, checked against the
  documented damage footprint (bridges at Ichar, Padum, Tipting, Chah, Pipcha).

A stage that cannot reproduce its own observable reports that it cannot, and says what
is missing. It does not supply a plausible number — the rule that killed `wse_m + 5.0`
applies to every stage added here.
