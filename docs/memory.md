# FloodSight — durable facts

Decisions and constraints that bind future work. Not a changelog — see
`findings_results.md` for what happened when. Read this before proposing changes.

---

## Derna's validation scores have a ceiling, and it is a scope decision

Derna is the only scenario with an independently observed outcome (Copernicus EMS
activation EMSR696), so it is the validation case. Its skill scores **cannot reach
1.0**, and that is by design, not a defect.

Derna 2023 was a compound event: the observed extent contains rainfall-driven
flooding (150–240 mm in a day over a 476 km² catchment) as well as the two dam-break
waves. FloodSight routes **only the dam break**. `SCENARIOS["derna"]` in
`src/data_fetcher.py` states this and ends: *"Read the score with that in mind rather
than tuning until it looks good."*

Concretely: `volume_mcm: 23.7` is Abu Mansour's storage and the model releases exactly
that. The published study puts total flood volume at 39 Mm³ (31–55) — reservoir plus
storm runoff at ~1,400 m³/s sustained inflow. That ~16 Mm³ gap is rain the model does
not simulate, deliberately.

**Adding a rainfall-runoff inflow to close the Bias gap is not a bug fix.** It was
proposed once and dropped for this reason. If it is ever wanted it is a scope change
to a different product, decided deliberately — not slipped in as an accuracy tweak.

Two further ceilings on the same scores:

- Copernicus EMS / SAR **over-maps** moisture-laden ground and **under-maps** urban
  and shallow flowing water. Both are live in a coastal city, so some "miss" area and
  some missed road links are not real flood.
- The published study validated against **building damage** (2,271 buildings, 78% of
  documented damaged) rather than extent alone — a more honest metric where SAR
  fails. FloodSight already carries buildings and road links, so that second metric
  is available if wanted.

Bias is the number to watch: it isolates extent under-prediction from location error.

## The GPU path is not a speedup on this machine

`src/m4_solvers/swe_2d_gpu.py` is complete and its tests pass. It stays opt-in
(`backend="cpu"` default, `run_pipeline` never passes the argument) because it is
**6.6× slower** than the windowed CPU path — measured, identical checksums.

Structural, not a tuning problem: the solver is float64 throughout and the GTX 1650
(Turing TU117) runs FP64 at 1/32 of FP32 rate; and the GPU path has no active window,
so it processes the full domain while the CPU solves ~6% of it.

Float32 would address the first and roughly double effective bandwidth, but the
scheme's well-balancedness depends on `h + z` cancellation, which is exactly where
float32 loses precision. Do not attempt it without gating on the Ritter analytical
benchmark (`src/m4_solvers/ritter.py`).

Do not re-propose "just turn on the GPU". It needs different hardware, or a windowed
float32 port proven against Ritter.

## The active window is exact, and must stay that way

The solver restricts each timestep to a bounding box around wet cells plus a halo.
This is an **optimisation, never an approximation** — a dry cell contributes
identically zero, so the windowed update reproduces the full-domain one bit for bit.

Anything that changes the halo, the rebuild interval, `cfl`, or the perimeter check
must keep `tests/test_swe_active_window.py` passing, which compares the two paths
with `==` rather than a tolerance. If a change makes bit-identity fail, the change is
wrong — do not widen the tolerance to accommodate it.

## Roughness is proxied, not classified

Manning's n comes from height above thalweg, overlaid with an urban mask derived from
the GHS-POP population grid. The population grid is a **proxy** for land cover — where
people live is built up. Published work on this event derived n from ESA WorldCover.

This is the honest use of data already on disk, not a claim of land-cover
classification. If accuracy work reaches the point where roughness dominates, real
land cover is the upgrade.

## GHS-POP tiles are per-scenario

The population tile is derived from the scenario bbox, not hardcoded. It was
hardcoded once, to a tile covering India, and Derna silently got an all-zero
population raster for it. Any new scenario outside 70–80°E / 30–40°N needs its own
tile fetched (30–170 MB, one time, cached under `data/population/_cache/`).

## There is one server, not two

One codebase, one `src/api/main.py`. The README says port 8000; `.claude/launch.json`
now agrees. Every frontend fetch is a relative `/api/...` path, so the app is
port-agnostic and any port works.

`_JOBS` is **in-memory only**. Run artifacts persist to `data/scenarios/<job_id>/`,
but nothing rehydrates `_JOBS` at startup, so a restart makes every completed run
unreachable through the API even though its files are still on disk. Restarting the
server is therefore destructive to in-flight and completed job *state*, though not to
the data. Fixing this needs a startup scan; it has not been done.

## Only the central breach arm has a timeline

All three arms are solved (`run_pipeline.py:500`), but the two outer arms are
**envelope-only** solves — the comment above them states that M5 and M6 process the
central arm to save time. Consequences that bind any future UI or analysis work:

- `cut_time_min` on road links, every `water_arrival_min`, and every
  `isolation_time_min` belong to the **central arm alone**.
- There are **no pessimistic or optimistic arrival times**. Anything drawing the
  ensemble as a *range in time* — the strongest idea in the timeline redesign — needs
  per-timestep depth kept for the side arms and the M6 cut-time pass run over all
  three, roughly tripling M6.
- What the ensemble *can* honestly show today is spatial: the `envelope-fill` layer
  already exists, so an arm switch showing "the pessimistic arm floods this much more"
  is free.

Also: the priority ranking's uncertainty band is `score × 1.15` and `score × 0.85`
(`src/m7_ranking/ranker.py:142`), with a comment admitting the real version would be
per arm. It is a placeholder and must never be presented as ensemble output.

## The interface accent is blue, and only ever on chrome

The map spends most hues already — depth blue, roads grey/amber/red, bridges purple.
An earlier pass therefore gave the interface *no* accent at all (neutral slate plus a
bone tone). That was overridden: the user supplied a dark command-centre mockup and
the brief wins.

The accent is `#2F6BFF`. Two rules keep it from colliding with the data:

- it is brighter and more saturated than every stop in the depth ramp, which tops out
  at `#1E3A8A`;
- it appears **only on chrome** — Play, active tab, playhead, focus, toggles — and
  never on a map layer or a lane fill.

Do not reintroduce a teal accent: the version before this one used one that sat
*inside* the depth ramp, so buttons and water competed.

## Anything that animates must survive a hidden tab

Runs take minutes and the poll loop is built for people tabbing away. In a hidden
document `requestAnimationFrame` never fires, CSS animations freeze in place, and
MapLibre will not load its style. Three separate defects came from this in one
sitting. The rule: **an animation may never be the only thing that makes an element
visible**, and every rAF-coalesced draw needs a synchronous hidden-document path plus
a `visibilitychange` repaint. Details and the specific fixes are in `ui.md`.

## Panels float over the map again — on the user's direction, kept safe by Grid

Reversed 2026-09-04. The user supplied a hand sketch (glassy floating windows over a
full-bleed map, referencing Spline/reactbits/lenis.dev as visual language) and asked
for the pre-columns floating look back, done properly this time.

The map (`#stage`/`#map`) is `position: fixed; inset: 0`, always full-bleed, a sibling
of the chrome, never inside a grid track. `#workspace` is a second, transparent
`position: fixed; inset: 0` layer on top: a CSS Grid with four named areas
(`top`/`left`/`right`/`bottom`), `pointer-events: none` on the grid itself so the
empty gutters click and drag through to the map, `pointer-events: auto` on each of
the four chrome children. The centre cell is never occupied, so it's always clear map.

This keeps the exact safety property the old boxed layout had — Grid will not place
two items in the same cell, so two floating regions still cannot collide — instead of
going back to the hand-positioned `absolute` stacks that caused the original bug
(see below). `#left-panel`/`#right-panel` are `align-self: stretch`, filling the full
row-2 height between the header and spine rows (a second user reference — proportion
blocks, not just the first rough sketch — asked for tall side panels, not short
auto-height ones), with `overflow-y: auto` as the safety net if content ever exceeds
that. The header and spine are themselves narrower than their full grid row and
centred (`justify-self: center` plus a `max-width`) rather than stretched edge to
edge, which is also what that second reference showed. Because the header/spine no
longer reach the viewport's side edges, MapLibre's own corner controls sit in
genuinely clear map at all four corners — no reserved-strip math needed.

New tokens: `--panel-gap` (18px, inter-panel gap and left/right outer inset),
`--panel-gap-v` (40px, top/bottom outer inset — deliberately larger, on request, so
the top and bottom bars read as clearly floating rather than flush), `--glass-bg`,
`--glass-border`, `--glass-blur`, `--glass-shadow` (neutral white/black-based
translucency — kept off-blue deliberately, see the accent rule above), `--ease-smooth`.

**What originally broke, preserved for context**: an earlier version positioned cards
absolutely over the map in three corner stacks; any two that grew past their allotted
space collided, and on the live page the legend was drawn over the validation panel,
hiding the road-link scores. That is why this rebuild uses Grid rather than hand-rolled
`fixed`/`absolute` math for the four chrome regions — the lesson was about manual
positioning, not about floating over the map per se.

Content also moved: the setup form (scenario, water level, failure mode, resolution,
reservoir fraction, run button, progress, breach-arm disclosure) came out of a modal
sheet and now lives permanently in the left panel; `#answer-card`/`#evac-card` (the
finding and the ranking) moved from left to right, joining event/validation/legend.
`openSetup`/`closeSetup` and the modal scrim no longer exist.

Full detail — exact grid-template-areas, the header/spine z-index fix needed because
`#app-header` now lives inside `#workspace`, the `#event-pop` pointer-events fix — is
in `ui.md`.

## Strict RAM & Background Process Hygiene (User Rule)

1. **Zero Auto-Starting MCP Servers**:
   - `code-review-graph`, `blender-mcp`, and `stitch` (`mcp-remote`) must **never** be auto-started in background.
   - Global config `C:\Users\chaud\.gemini\config\mcp_config.json` and workspace `.agents\mcp_config.json` are set to empty `{ "mcpServers": {} }`.
   - Never launch them unless explicitly requested by the user.

2. **No Lingering Node/Playwright Processes**:
   - Do NOT run background browser subagents or Playwright/Chrome-DevTools sessions unless explicitly ordered by the user.
   - Any driver processes (`ms-playwright-go`, `chrome-devtools-mcp`, `node.exe`) must be terminated immediately after an explicit subagent run.

3. **Only FloodSight Server Allowed**:
   - Only the single FloodSight FastAPI server (`python.exe -m uvicorn src.api.main:app --port 8000`) should be active. All other background python/node sidecars must stay terminated.

## Annamayya's event clock is T=0 = dam washout (06:30 IST), not overtopping (05:45)

**Corrected 2026-09-11 — the entry this replaces had T=0 wrong and it was
never actually reconciled against the numbers next to it.** The old text
below claimed T=0=05:45 (overtopping) AND Pincha at "T-150 min" (03:15) in
the same breath; 03:30 (the actual sourced Pincha time, EVD-04) to 05:45 is
135 minutes, not 150. Nobody had checked the arithmetic before this session.

The evidence — `data/evidence/annamayya_event_evidence.json`
(`event_clock` block) and `ANNAMAYYA_DATA_AUDIT.md`'s EVD-01..28 table —
has THREE candidate "T=0" moments, not one: EVD-16 overtopping initiation
(05:30-06:00, a range), EVD-17 full washout (06:15-06:30, a range), and
the MHA's own point value for the latter (06:30). **User decision**: T=0 is
the dam failure/washout event, using the MHA point value 06:30 IST — not
overtopping. This is now the single value in `event_clock.origin_iso`,
served by `src/scenarios.py:load_event_clock()`, read from the evidence
file. Nothing hardcodes it elsewhere; `frontend/map.js`'s 4 hardcoded
`event_clock` blocks (rishiganga/derna/south_lhonak/annamayya, none of which
had a citable source for their origin times) are deleted.

Consequences that bind future work on this scenario:

- Pincha ring-bund failure (EVD-04, OBSERVED, 03:30 IST) is `t_s = -10800`
  (180 min before T=0), not `-150`. `src/data_fetcher.py`'s
  `cascade.upstream.lead_time_s` was `9000.0` and is now `10800.0` to match.
  Do not re-derive "150" from any of the three T=0 candidates — it never
  matched any of them (135 min against 05:45, 150 against 06:00, 180 against
  06:30 — the code had picked 05:45 for T=0 but kept the 150-based lead time
  anyway, an internal inconsistency present from the start, not introduced
  by this fix).
- Overtopping initiation (EVD-16) is kept as its own timeline event, earlier
  than T=0, not collapsed into it — the reservoir was already overtopping
  for perhaps 30-60 minutes before the embankment fully washed out.
- EVD-21 (Cheyyeru gorge exit) and EVD-22 (Togurupeta), sourced at
  06:15-06:25, land *before* T=0=06:30. **User-confirmed physical reading**:
  these are arrivals of the pre-washout overtopping discharge (crest flow
  during the ~30-60 min the dam was overtopping but not yet gone), a
  distinct and earlier pulse from the post-washout breach wave that reaches
  the same points afterward — not measurement error, and not to be
  "corrected" to positive t_s.
- The full 9-event chronology (Pincha failure → overtopping → pre-washout
  arrivals → washout/T=0 → Mandapalli → Pulapathur → Gundlur → Nandalur →
  far-field) lives in `annamayya_event_evidence.json`'s `event_clock.
  timeline_events`, each with `t_s`, `t_uncertainty_s`, a historical
  time/range, `classification`, and `source`. This is meant to be extended,
  not re-derived from scratch, if a future EVD record changes.

Separately, unrelated to the clock fix but found while researching it: the
prior claim that "Delft3D-FLOW is explicitly positioned as a precomputed
synthetic benchmark reference" was itself the FS-15 fabrication — the
"Delft3D" trace and its "±0.14 m RMSE" band were the Ritter analytical curve
relabelled twice over (`y: h_analytical` for both series), not a Delft3D
result under any framing. Deleted from `frontend/charts.js`. See
`findings_results.md`'s 2026-09-11 entry for the fuller P0/P1 record.


## The DEM fill is not trustworthy for any scenario yet — measured, not argued

Established 2026-09-11 by P2 Gate 1 (`run_pipeline.py:442-475` diagnostic, all 7
scenarios). Full numbers in `findings_results.md`.

**rishiganga fills the gorge, not the impoundment, and the guard does not catch it.**
`build_stage_storage` returned 9,672 MCM against a configured `v_frl_mcm` of 0.15
(**64,483x**) and a dam height of 756.6 m against a configured 70 m. The pool's `z_min`
came out **743.6 m below the declared `z_bed_m`**. The confinement guard passed because
the pool never touches the raster edge — it just fills a canyon that stays in-domain.
Edge-touching is therefore **not** a sufficient confinement test.

This is the measured instance of the asserted-`wse_m` problem already recorded above:
`run_pipeline.py:417` takes `wse_m` straight from `cascade_cfg["reservoir"]["z_crest_m"]`
with no terrain reconciliation.

**How to apply:** do not promote the DEM-derived stage-storage curve over the
analytical power law (P2 Gate 2's plan) until the overrun is root-caused — candidates
are seed placement, barrier emplacement, or the asserted `wse_m`. "The DEM fill is the
physically honest one" is true in principle and currently false in practice.

**Do not read 3 of the 7 Gate 1 results as scenario defects.** derna failed on a bug in
the throwaway diagnostic script (missing output dir), not on physics — it is the one
scenario that passes `test_run_lifecycle.py`. south_lhonak and annamayya died upstream
in roads/terrain fetch before reaching the check. All three are **unknown**, and the
sweep ran at `coarsen=16`, which smooths barriers and may itself cause edge-touching.

## Kosi 2008's validation raster on disk is the wrong event

Established 2026-09-11, verified directly against
`data/validation/gfd_meta/dam_events.csv`. `DFO_3382_From_20080922_to_20080929.tif` is
**ID 3382, India, 20.95 N / 84.41 E, began 2008-09-22, cause "Dam release and Heavy
Rain"** — that is **Odisha, ~700 km from the Kusaha breach**, starting 35 days after
it. Almost certainly the Sept 2008 Mahanadi/Hirakud release event.

**Two repo documents assert otherwise and are wrong:**
`FLOODSIGHT_DEEP_REVIEW_2026-09-11.md:321` ("covers the 2008 window and is **already on
disk**" — the entire stated basis for "Kosi is the shortest path to a second validated
case", repeated at `:328` and `:626`) and `data/validation/README.md:51`.

**Why this is dangerous rather than merely wrong:** the GFD regional MODIS tile spans
72-91 E, so it *does* overlap Bihar, and carries ~28,000 flooded pixels inside a
Kosi-fan box. Scoring CSI against it returns **plausible-looking numbers from the wrong
flood on the wrong date** instead of failing loudly.

The correct event is **DFO 3365** (Nepal, 26.95 N / 84.92 E, began **2008-08-18** —
the exact breach date), listed in `gfd_available_events.csv:511` but **not on disk**;
32 MB download. It was filtered out because DFO labels its cause "Heavy monsoon rains"
rather than an embankment cause, so the repo's dam-event filter will keep missing it.
Its window is a 37-day maximum-extent composite, not a snapshot.

## Kosi 2008 was not a flood-magnitude failure, and has no published breach coordinate

Established 2026-09-11 from Sinha et al. 2013 (*Geology* 41(10):1099) and Sinha et al.
2014 (*Geomorphology* 216:157-170), both read in full.

The breach occurred at **~4078-4320 m3/s against a ~27,000 m3/s design capacity** —
roughly **15% of design, 40% of bankfull** (bankfull 7458 m3/s at Birpur). Sinha 2014
states outright the avulsion "was not caused by a large flood event". Mechanism is
**toe/spur erosion plus seepage on a perched channel** whose bed sits ~4 m *above* the
adjacent floodplain, after the thalweg migrated against the eastern embankment from
2000 onward — **not overtopping**, and freeboard was never exceeded.

**How to apply:** any Kosi scenario driven by a monsoon-peak hydrograph is physically
wrong, and `failure_mechanism` for it is a seepage/lateral-scour case, not the
overtopping kernel. Also: **no published breach lat/lon exists** in either paper — the
best available is a village centroid with two similarly-named candidates ~20 km apart
(Paschim Kasuha vs Purbakushaha). Do not present a derived coordinate as sourced.
