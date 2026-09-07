# FloodSight — findings and results

Append-only log. Every number here came from a run whose output was read; anything
extrapolated rather than measured is labelled as such.

---

## 2026-09-03 — Map overlays were rendering the solver's grid cells

**Symptom.** Flood polygons and the hit/miss validation overlay looked like squares on
the map. An earlier attempt had reported the problem fixed; it was not.

**Root cause.** `rasterio.features.shapes` puts every vertex on a raster grid line, so
the output is a staircase by construction. Pre-blurring the raster only moves *which*
grid lines. Two independent artefacts, needing two different measurements:

- A **staircase**, whose signature is perimeter — a cell-edge path along a diagonal is
  the taxicab distance, up to √2 longer than the line it traces.
- **Fragmentation** — the fringe of an agreement raster is scattered lone cells, each
  vectorising into its own cell-sized blob.

**What did not work, and why it looked like it did.** The previous fix used
`buffer(+r).buffer(-r)`. That is a morphological *closing*: it fills concave notches
and leaves convex corners — the ones you actually see — untouched. Worse, it replaces
each 90° corner with a short arc, which drives a **vertex-angle** metric to 0% while
the boundary stays just as staircased. My own first metric was vertex angles, and it
passed the broken code. Perimeter excess is what sees through it.

**Measured** (synthetic diagonal flood band, 30 m cells):

| variant | perimeter excess | vertices |
|---|---|---|
| no smoothing (control) | 1.378 | 2,299 |
| previous "fix" (buffer closing) | 1.210 | 27,395 |
| current | **1.005** | **193** |

Agreement overlay, speckled fixture: 295 sub-polygons → **3**.

**Fix.** `simplify(cell/3)` instead of the buffer closing; `order=1` upsampling
instead of nearest-neighbour (which replicated each cell into an identical block and
gave the blur no sub-cell detail to work with); sigma scaled to `display_scale`;
`connectivity=8`; threshold 0.5 not 0.45 so HIT/MISS/FALSE stay mutually exclusive.

**Verification.** 12 tests in `tests/test_overlay_geometry.py`, each metric paired
with a control that must fail. Mutation-tested by reverting each change individually —
4 of 6 caught. The 2 misses are real findings, not gaps: on the depth path `sigma` and
`connectivity=8` are measured no-ops, because bilinear upsampling alone already
removes isolated spikes there.

---

## 2026-09-03 — Solver was 6.6× slower than it needed to be

**Measured first.** Across a completed Derna run's 13 snapshots: at peak flood
**1.09% of cells are wet**, and their bounding box is 6.4% of the domain. The solver
was updating all 637k cells every step.

**Fix — active window.** Each step now solves a box around the wet region plus a
6-cell halo. **Exact, not an approximation**: a dry cell contributes identically zero
(with `hL = hR = 0` the Rusanov flux vanishes and the bed-slope source carries
`h_bar = 0`), so the windowed update reproduces the full-domain one bit for bit
provided no wet cell sits within the stencil width of the box edge. CFL bounds front
motion at 0.35 cells/step, so a 6-cell halo covers the 4 steps between rebuilds; a
perimeter check every step catches any violation regardless.

**Measured**, real Derna DEM, 600 s simulated:

| grid | cells | full domain | windowed | |
|---|---|---|---|---|
| 171 m | 17,835 | 5.59 s | 2.06 s | 2.7× |
| 114 m | 39,928 | 13.98 s | 2.67 s | 5.2× |

Checksums identical to 11 significant digits at every resolution. ~1.3 s of each
windowed figure is one-time numba JIT, so steady-state is better than the ratio shows.
28 m: 900 s of simulation in 50 s (was hours). The win grows with domain size — the
flood covers a fixed physical area, so a finer grid means a larger dry fraction.

**Verification.** 9 tests in `tests/test_swe_active_window.py`. Bit-identity asserted
by running both paths in subprocesses and comparing with `==`, not `approx`.
`FLOODSIGHT_FULL_DOMAIN=1` selects the old path and exists only for that comparison.

One subtlety worth recording: the simulated **state** is bit-identical, but
`volume_outflow` and `volume_clipped` differ in the last bits (6.03e-17 vs 6.25e-17 m³
on one fixture) because they are running sums over a different set of boundary faces
and hit a different rounding order. The test tolerances those two against *injected
volume*, not against themselves — a self-relative test called two spellings of
numerical zero a 3% divergence.

### What did NOT work

**GPU — measured and rejected.** `swe_2d_gpu.py` exists, is complete, and passes its
5 tests. It is **6.6× slower** than the windowed CPU path: 22.48 s vs 3.41 s at 114 m,
identical checksum. At 28 m it failed to finish 900 s within 120 s where the CPU took
50 s. Two compounding reasons: the solver is float64 and the GTX 1650 runs FP64 at
1/32 rate, and the GPU path has no active window so it grinds the full domain.
Not wired on. Float32 would fix the first but risks well-balancedness (the scheme
depends on `h + z` cancellation) and would need gating on the Ritter benchmark.

### Measured but not attempted

- **Fuse `_rhs` into one njit kernel** — profiling at 28 m shows ~47% of compute is
  Python glue; `np.pad` alone is 5,648 calls / 0.78 s. Worth roughly 1.9×. Not done:
  it is a ~150-line rewrite of the well-balanced reconstruction, the one function
  guaranteeing shoreline correctness.
- **Tighter-than-bounding-box active set** — the Derna flood is a diagonal ribbon, so
  its bbox is ~6× larger than the wet set. Composes best after fusion cuts per-call
  overhead.
- **`prange`** — *reasoned, not measured.* The window made arrays smaller (~2.5k
  cells) and the existing code comment records it was already slower at 17.8k.

---

## 2026-09-03 — Four data and physics bugs

**Population was silently zero for Derna.** `_GHSL_TILE` in `src/data_fetcher.py` was
hardcoded to `R6_C26` (70–80°E) with the comment "contains both scenarios" — written
before Derna was added at 22.6°E. Derna clipped against a tile that does not contain
it and produced an all-zero raster, which is why the priority ranking read
"0 people · not computed". Tile is now derived from the scenario bbox.

Measured: **78,488 people in the Derna AOI, was 0.** Verified phutkal and rishiganga
still resolve to `R6_C26` so the existing cache stays valid.

**Breach ensemble.** `route_breach` reads `breach_width_m`, `side_slope_hv` and
`formation_time_h` — never `Q_p` — so only the third of these was physics:

| | before | after |
|---|---|---|
| von Thun `Q_p` | 304,388 m³/s | 18,589 |
| MacDonald `Q_p` | 6,084 m³/s | 7,384 |
| MacDonald breach width | **6.2 m** | **26.1 m** |

von Thun's `Q_p` came from a weir rearrangement that ignores reservoir drawdown — it
would empty 22.5 Mm³ in 84 seconds. MacDonald's used M-L's coefficient with a
head-only exponent, so two dams of the same height but different volume returned the
same peak. MacDonald's *width* was back-solved from `Q_p` through the same weir
relation — the identical category error — giving 6.2 m for a 73 m dam, and that fed
the router. Replaced with M-L's own eroded-volume equation.

Ensemble now has three distinct sensible geometries: Derna 26 / 103 / 237 m, same
ordering for Rishi Ganga and Phutkal.

**Urban roughness.** Elevation-banded Manning gave coastal Derna channel roughness
(n=0.032) because the city sits at low elevation — under-roughening the flood exactly
where the observed misses are. Now overlays an urban mask from the population grid
(n=0.08), coarsen-aware via `maximum_filter` so settlements are not lost to striding.
Measured active on 2.8–4.1% of Derna cells depending on preset. Depended on the
population fix above to do anything at all.

**API duration.** `src/api/main.py` defaulted `duration_s` to 3600 while
`run_pipeline`'s own default was already 7200 — the API was halving it. Water reaches
Derna at T+45 and the sim was stopping at T+60.

### Result, Derna at 114 m

| | before | after |
|---|---|---|
| Bias | 0.473 | **0.599** |
| POD | 0.340 | 0.377 |
| CSI | 0.300 | 0.308 |
| FAR | 0.282 | 0.372 |
| simulated area | 5.4 km² | 6.8 km² |
| people at risk | 0 | **2,592** |
| top AOI score | 0.46 | 0.81 |

Tests: **39 → 60 passing.**

### A recommendation of mine that was wrong

I ranked "add rainfall-runoff inflow" as the second-biggest accuracy win, on the
grounds that released volume (23.70 Mm³) is far short of the published total flood
volume (39 Mm³). It is not a bug. `SCENARIOS["derna"]` documents that FloodSight
routes only the dam break by design, and the comment ends *"Read the score with that
in mind rather than tuning until it looks good."* Dropped rather than implemented.

---

## Unfinished

- **`_JOBS` rehydration.** `_JOBS` in `src/api/main.py` is in-memory only, but run
  artifacts persist to `data/scenarios/<job_id>/` — around 70 of them. Nothing
  rebuilds `_JOBS` at startup, so every completed run becomes unreachable after a
  restart. A startup scan would make restarts non-destructive.
- **`_rhs` fusion**, as measured above.
- **`data/scenarios/test_run/max_depth.tif`** is EPSG:4326 with degree cells, and the
  pipeline rejects geographic DEMs upstream — a stale artifact, useless as a fixture.


---

# 2026-09-04 — Frontend rebuilt around the timeline ("direction B")

The three-column dashboard was replaced with a two-region layout: full-bleed map on
top, a timeline "spine" underneath. Chosen from five sketched directions; the spine
was picked because every figure the run produces is a function of time, so the
scrubber, the outflow hydrograph, the road colours and the isolation callout were
four panels showing one variable.

## What was built

| file | change |
|---|---|
| `frontend/index.html` | rewritten — two regions, setup sheet, event popover |
| `frontend/styles.css` | rewritten — new token set, 3-state theming kept |
| `frontend/spine.js` | **new** — three lanes (flow / events / roads) on one domain |
| `frontend/ui.js` | **new** — sheet, legend, download menu, popover, answer card |
| `frontend/map.js` | patched — spine hooks, guards, wording, several real bugs |
| `frontend/charts.js` | one guard: `renderDemoHydrograph` no longer targets a removed div |

Panels removed from the surface: left rail, right rail, hydrograph panel, exposure
counter grid, isolation callout, six emoji section headers, four `COMPUTED LIVE`
badges, `ANALYST INPUT`, `PS DELIVERABLE (iii)`.

Input is the existing `<input type="range">`, laid transparently over the lanes at
`opacity: 0`. Drag, arrow keys, Home/End and screen-reader announcement therefore
come from the platform; there is no pointer-handling code in `spine.js`.

## Bugs found while wiring it — all pre-existing

1. **`roadsGeoJSON` was never declared.** `map.js` is `"use strict"`, so
   `roadsGeoJSON = await roadsResp.json()` threw `ReferenceError` inside the road
   try/catch on **every run**. The catch logged `Road timeline not available` at
   `warn` level and the road layer stayed empty. There is a comment at
   `map.js:25` recording the identical defect being fixed for `currentBasemap` —
   the second variable was missed. Declared at `map.js:28`.

2. **`_loadDemoResults()` could wipe a finished run.** MapLibre does not load its
   style while the canvas is hidden, so a run started and completed in a background
   tab is followed by the map's `load` event firing on return — which called
   `_loadDemoResults()` and reset the results to zeros. Observed directly: card read
   `0 people` and `Run a simulation to see the ranking` while `simulationResults` held
   33 features. Now guarded.

3. **`requestAnimationFrame` work never ran in a hidden tab.** Both the count-up and
   the timeline draw were coalesced into a frame that never arrives. The polling loop
   in `runSimulation` is explicitly built for people tabbing away during a long run,
   so this was reachable in normal use. Fixed three ways: paint synchronously when
   `document.hidden`; repaint on `visibilitychange`; cancel the stale pending frame id
   on the way back in, since it otherwise made `render()` think a repaint was queued.

4. **CSS entrance animations froze mid-flight** when the tab was hidden during the
   pin stagger, leaving pins at `opacity: 0` permanently. Measured opacities across
   the eight pins in one frozen state: `0.94, 0.86, 0.73, 0.51, 0.17, 0, 0, 0`.
   Fixed by removing opacity from `@keyframes pin-in` entirely — the entrance is
   transform-only, so a frozen animation still leaves a visible pin.

5. **Document scrolled out of the viewport.** `openSetup()` called `focus()` on the
   first field, which scrolled the document by 104 px and took the header off screen
   with no way back. Fixed with `focus({preventScroll: true})` plus
   `html { overflow: hidden }`.

## Verified against a real run

Rishiganga, `coarsen=6` (170.1 m cells), job `8d026d4f`, run start to finish with the
tab hidden the whole time:

| | value |
|---|---|
| status | `Done.` |
| people in flood path | 1,573 |
| buildings flooded | 194 |
| villages ranked | 33 (ranks 1, 2, 3 correct) |
| event pins | 8, all `opacity: 1` |
| roads ribbon path | 3,637 chars, non-degenerate |
| frames | 25, domain T+0..T+120 |

Scrubbing, measured: at **T+0** the card reads `0 of 32` roads cut and `0` villages;
at **T+45** it reads `18 of 32` and `11`; playhead transform is exactly
`translateX(50%)` at T+60 of a 120-minute domain, and 8 pins carry `.pin-passed`.

Playback: `data-playing="true"` gives the playhead
`transition-property: transform`, `transition-duration: 2s`, matched to the 2000 ms
frame interval, so discrete frames read as one continuous sweep. Scrubbing has
`0s` — instant, as it should be.

## What did not work / was rejected

- **Ensemble ranges on the event pins** — the whole reason direction B looked strong.
  All three arms *are* solved (`run_pipeline.py:500`), but the two outer arms are
  **envelope-only**; the comment above them says downstream M5/M6 process the central
  arm to save time. So `cut_time_min` and every arrival time in the run output belong
  to the central arm alone and **there are no pessimistic/optimistic arrival times to
  draw**. Not implemented. The cheap substitute is the existing `envelope-fill` layer
  with an arm switch on the map.
- **Filling the spine left-to-right as the pipeline progresses.** Reads well, lies:
  M3/M4/M5/M8 are pipeline stages, not simulated minutes. Progress kept its own bar.
- **Auto-opening the popover on every scrub** (the old callout behaviour). A panel
  that appears unbidden while dragging is noise. The popover is now click-only;
  `_checkIsolationAtFrame` just marks the current row in the ranking.
- **`switchBasemap` before the style loads** silently does nothing — pre-existing
  early return on `!map.isStyleLoaded()`. Left alone, but a click during load is a
  no-op with no feedback.

## Not a defect, wasted time confirming it

A black map during verification was **OSM tile throttling**, not the rebuild. Proof:
the identical build rendered terrain, and switching to the ArcGIS satellite basemap
rendered immediately while `tile.openstreetmap.org` requests returned
`transferSize: 0`. Already recorded in `ui.md` as an environmental condition.

## Unfinished, from this work

- **Villages report `isolation_time_min ≈ 0.1`.** At T+10 the card reads 11 villages
  cut off but 0 roads cut, which is self-contradictory on its face. The UI is
  reporting the run output faithfully; the ~0 isolation times come from the pipeline
  and have not been investigated.
- **The evacuation-window sign is still untraced.** The popover now sidesteps it by
  writing the relationship out from the two timestamps rather than printing the
  signed `evacuation_window_min`, but the underlying field is still suspect.
- **Only 2 of 8 pins get labels** at a 120-minute domain, because events bunch in the
  first 30 minutes and `LABEL_GAP` is 8.5% of the width. Readable, but sparse.


---

# 2026-09-04 (later) — Shell rebuilt as three columns

The floating-card layout was replaced with three real columns after the live page
showed several elements drawn on top of each other. Direction taken from a supplied
mockup (dark command centre); the mockup's left icon rail was **not** copied — six
items, only two of which exist in this app, duplicating the top tabs.

## The overlap bug was structural, not cosmetic

Cards were absolutely positioned over the map in three stacks (`.stage-tl`,
`.stage-tr`, `.stage-br`). Any two that grew past their allotted space collided. On
the live page the legend was drawn over the validation panel, hiding the road-link
scores entirely.

Fixed by construction: `#workspace` is now
`grid-template-columns: var(--panel-w) 1fr var(--panel-w)` with real scrolling
panels. **Audit after the change, measuring every panel and card pair-wise for
intersection: `overlaps: []`.**

## Other real bugs fixed in the same pass

- **Mixed Latin/Arabic place names rendered scrambled.** `village_name` is the raw
  OSM `name` tag (`"AOI01 درنة Derna"`). With no bidi isolation the reordering
  algorithm ran across the rank number and the score sitting either side of it, so
  all three scrambled together. Fixed with `dir="auto"` plus
  `unicode-bidi: isolate` on `.p-name`, `.pop-name` and `#callout-village`.
- **A stray × floated in the middle of the map.** The popover's close button was
  positioned absolutely inside a host whose own box had collapsed.
- **The roads ribbon was drawn under the transport controls.** `.spine-body` had no
  `min-height: 0`, so the lanes overflowed their grid row by 34 px. Measured before:
  wrap 185 px, body 219 px. After: roads lane `800..826`, footer top `840`.
- **Event labels collided with the flow lane.** They alternated above/below the axis;
  the "above" row landed on the hydrograph curve. Now two stacked lines (time above
  name) always below the axis, and the events lane has a fixed 66 px row.

## Palette changed on the user's direction

The previous pass used no interface accent at all — chrome was neutral slate plus a
bone tone — on the reasoning that the map already spends every hue. The supplied
mockup pins a blue command-centre look, and the brief wins. Accent is now `#2F6BFF`,
chosen brighter and more saturated than every stop in the depth ramp (which tops out
at `#1E3A8A`) and confined to chrome: Play, active tab, playhead, focus, toggles.
The road colours are still shared verbatim between the ribbon and the map.

## Map tilt — my change, reverted twice

I flattened the map to `pitch: 0, bearing: 0`, reasoning that a tilted view
foreshortens the depth polygons where the water is deepest. Rejected. I then offered
`pitch: 30, bearing: 0` as a compromise. Also rejected. Back to the original
`pitch: 40, bearing: -10`, which is what it should have stayed as — the oblique view
reads the valley the water runs down, and that is what the map is for here. Recorded
because it is exactly the kind of change that gets re-proposed.

## A bug I introduced, in my own responsive CSS

Below 820 px the spine lanes collapsed to **40 px wide**. `.spine-gutter` is
`display: none` at that width, which leaves the wrapper with one item and two tracks
(`0px 1fr`) — and auto-placement dropped that item into the zero-width first track.
Setting `--gutter-w: 0px` was not enough; the track itself has to go
(`grid-template-columns: 1fr`). Measured before: `wrapCols "0px 634.4px"`,
`bodyW 40`. After, at 1512 px: `bodyW 1408`.

Same query also had the left panel as a full-height absolute overlay, which covered
the map with nothing to dismiss it. Now it stacks above the map with a `42vh` cap.

## Verified

Viewport 1512×900, job `8d026d4f` replayed through the real render path:
`pitch: 40`, `bearing: -10`, `overlaps: []`, `bodyW 1408`, both themes, spine lanes
measured clear of the footer, ranking header no longer wrapping in a 268 px column.

---

# 2026-09-04 (later still) — Boxed columns reversed back to floating glass panels

The three-column shell from the previous entry was reversed on explicit user
direction: a hand sketch (glassy floating windows over a full-bleed map, referencing
Spline/reactbits.dev/lenis.dev) plus, later in the same session, a second literal
proportion mockup after the first pass's sizing missed what was meant.

## What was built

Delegated to the `implementer` subagent (4 files, full rewrite): `frontend/index.html`,
`styles.css`, `map.js`, `ui.js`. `spine.js` untouched — confirmed by grep, zero-line diff.

- Map (`#stage`/`#map`) now `position: fixed; inset: 0`, permanently full-bleed, a
  sibling of the chrome rather than a grid track.
- `#workspace` became a transparent overlay `position: fixed; inset: 0` Grid
  (`z-index: 30`) with four named areas; the centre cell is never occupied, so it's
  always clear map. This keeps the same Grid-based non-overlap guarantee the boxed
  layout had (see the two entries above) instead of returning to the hand-positioned
  `absolute` stacks that caused the original overlap bug.
- Setup form moved out of a modal sheet (`#setup-sheet`, `#setup-scrim`,
  `openSetup`/`closeSetup` all deleted) into a permanent left "EMULATION" panel.
  `#answer-card`/`#evac-card` moved from left to right, joining event/validation/legend
  as "RESULTS".
- Glassmorphism added from scratch — the codebase had **zero** `backdrop-filter`
  usage before this. New tokens `--glass-bg/-border/-blur/-shadow` (neutral
  white/black-based, deliberately off the `#2F6BFF` accent), applied to all four
  floating regions plus the popover and map controls.
- Terrain: new `raster-dem` source (AWS Terrarium tiles, no key) feeding
  `map.setTerrain({exaggeration: 1.2})` plus a `hillshade` layer, added as the first
  `load` step so layer order needs no `beforeId`. `flyTo` calls given consistent
  cubic-ease-out timing (400ms → 1400ms).

Two real bugs the implementer caught and fixed on its own before reporting back
(verified, not just claimed): moving `#app-header` inside `#workspace` would have
made the pre-existing `body:has(.page:not(.hidden)) #app-main{display:none}` rule
hide the header on the Solvers/Alerts/About tabs too — fixed by scoping that rule to
the dashboard-only regions and raising `#workspace` above `.page`'s z-index. And
`#event-pop` needed an explicit `pointer-events:auto` since `#workspace`'s
`pointer-events:none` (for click-through gutters) inherits into it otherwise.

## Two correction passes after the first build

**Sizing was wrong the first time.** First pass made the header/spine full-width
flush bars with a uniform 18px inset, and the side panels auto-height-and-capped
(reasoning: reserve a clear strip at the bottom of each column for MapLibre's own
controls). User's first correction ("decrease the size of the bars, add large padding
top and bottom") was read as: shrink `--header-h`/`--spine-h` (56→44, 254→200) and add
a bigger vertical outer inset (`--panel-gap-v: 40px` vs the 18px horizontal one). Not
what was meant.

**A second, literal proportion mockup** (solid blocks at measured relative sizes,
supplied because the padding fix still wasn't it) showed the actual intent: header and
spine centred and narrower than their row — not edge-to-edge — and the side panels
running the *full* column height, not a short content-sized box. Implemented directly
(no further delegation, single-file CSS + one map.js line, well under the
write-it-myself threshold):

- `#app-header`/`#spine` — `justify-self: center` + `max-width` (920px / 1180px)
  inside their still-full-width grid row.
- `#left-panel`/`#right-panel` — `align-self: stretch` (Grid default) replacing the
  auto-height + `max-height` calc entirely.
- `.map-tools` bottom offset simplified from a spine-clearance formula to a flat
  `var(--panel-gap-v)`, and `NavigationControl` moved back to `"top-left"` — both
  became unnecessary/wrong once the header/spine stopped reaching the side edges,
  because doing so left all four viewport corners genuinely clear map.

This second pass reads as a net simplification, not just a different tradeoff: the
reserved-clearance math it deleted only existed to solve a problem the short-panel
layout created in the first place.

## Verified

Overlap audit (same `getBoundingClientRect` pairwise-intersection technique as the
entry above), both passes, both themes:

| | first pass, 1280×720 | second pass, 1512×900 |
|---|---|---|
| `overlaps` | `[]` | `[]` |
| header | `x18 y40 w1244 h44` | `x296 y40 w920 h44` (centred, not edge-to-edge) |
| left panel | `w268 h270` (auto-height) | `w336 h540` (full column height) |
| spine | `w1244 h200` | `w1180 h200` (centred) |

Console: only the pre-existing, documented-harmless `InvalidStateError` (placeholder
depth-raster image). One environmental non-issue re-confirmed: OSM tiles took several
seconds to appear after each reload/resize — matches the tile-throttling condition
already recorded in `ui.md`, not a regression; terrain/hillshade rendered correctly
once tiles arrived, in both themes, on the Rishiganga scenario.

Not independently re-verified after the second pass: the ≤1100px/≤820px responsive
breakpoints (rewritten by the implementer for the overlay-grid structure, screenshot-
checked once during the first pass at desktop width only) and the setup form's
scroll behaviour now that its container is full-height rather than auto-height.
