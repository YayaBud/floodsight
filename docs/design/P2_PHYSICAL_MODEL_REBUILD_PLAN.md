# P2 — Physical event-generation chain rebuild: plan

Status: **plan only, no execution**. Written against the 2026-09-11 audit (full file/line
citations below), not against the earlier review doc's summaries — several of the
doc's characterizations turned out to be wrong or incomplete once the actual code was
read; corrections are called out inline where they change scope.

Scope boundary from the directive: 2-D SWE solver internals (Audusse reconstruction,
MUSCL/minmod, SSP-RK2, Rusanov, friction, active window) are **out of scope** except for
the one named extension (multi-inflow). Everything upstream of the solver — impoundment,
lake state, trigger, mechanism, breach, hydrograph — is in scope.

---

## 0. What the audit actually found (ground truth, supersedes the review doc)

Read `D:\sih_work\floodsight\FLOODSIGHT_DEEP_REVIEW_2026-09-11.md` §O/§P for the prior
review pass; read this section for what's different now that the code itself has been
read line-by-line.

**Two live, disconnected integrators, not one broken one.**
`simulate_prebreach_rise` (`cascade.py:387-523`) integrates inflow→spillway→storage→level
up to t≈0, then stops. `simulate_generic_cascade` (`cascade.py:177-364`) starts a
*separate* integration at `z_crest + 0.05` (`cascade.py:277`) — 5 cm over crest,
unconditionally, no trigger test. The seam between them is not an oversight: it is
logged and deliberately left open (`cascade.py:502-512`, "Do NOT tune to close it").
Rebuilding "one continuous state" (directive item 4) means replacing both call sites
with a single integrator that carries `INTACT → OVERTOPPING → BREACHING → BREACHED`
through the same state arrays — this is a real rewrite of `cascade.py`, not a glue
function between the two existing ones.

**The breach kernel is not trapezoidal.** `cascade.py:295` is a pure rectangular weir,
`Q = Cd·B(t)·h^1.5`, no side-slope term — the "trapezoidal" comment (`cascade.py:289`)
mislabels it. `ensemble.route_breach` (`ensemble.py:179-190`) *is* trapezoidal
(`Z·h^2.5` term at `ensemble.py:186`) and uses linear invert erosion, not `^0.8`. Two
different erosion laws already coexist in the `m3_breach` package before
`event_graph.py`'s third (linear width, no invert erosion at all,
`event_graph.py:232-244`) is even considered.

**Ensemble independence is compromised, not just labeled loosely.** Von Thun's `Q_p` is
hardcoded to Froehlich's formula inside `von_thun.py:72`, with a comment explaining why
(the physically-correct weir-rearrangement form was numerically absurd — 304,000 m³/s
for Derna). Consequence: the "3-arm ensemble" has **2 distinct peak-discharge values**,
not 3 — pessimistic/optimistic are decided by MacDonald's position relative to a tied
Froehlich/VonThun pair, then **re-labeled a second time** after routing by whichever arm
comes out highest post-`route_breach` (`ensemble.py:242-261`). MacDonald's own `t_f` is
Froehlich's formula too (`macdonald.py:84-85`, "stand-in"). Effectively one erosion-time
model and one discharge model are shared across all three "independent" methods, with
only breach width and side-slope differing.

**`cascade_config_to_graph` is not dead-but-correct — it is dead-and-wrong.** It computes
`alpha_m3_per_m = V_FRL` directly (`cascade.py:600`) instead of `V_FRL / (z_frl-z_bed)^exp`
— roughly 2900× off for Annamayya — and omits `invert_elevation_m`, which zeroes
`event_graph`'s breach head permanently. Wiring it up as a pure migration step (the
review doc's original P2-1 framing) would not have reproduced the live cascade even
approximately; it would have produced a near-zero breach. This is why the directive's
instruction to compare kernels term-by-term before choosing was correct — the two
kernels were never actually connectable as written.

**`event_graph.py` has the only real computed trigger in the codebase**
(`event_graph.py:234`, `stage >= overtopping_elevation_m`, latched from live integrated
stage) but it is also mass-bounded per step (`event_graph.py:244`,
`br = min(br, max(0.0, incoming_q + storage/dt))`) — a check `cascade.py` lacks entirely
(its `max(1e-3, v_res[i]+dv)` floor silently manufactures mass on overdraw,
`cascade.py:299`). Neither module has both a real trigger and a real erosion law and a
real mass bound at once. None of the three existing kernels (`cascade.py`,
`event_graph.py`, `ensemble.route_breach`) is simply "the good one" — each is missing a
different piece the others have.

**Stage-storage: two representations that never meet, confirmed structurally, not just
by absence of wiring.** `build_stage_storage` (`fill.py:49-189`) is real DEM-integrated
physics (100-step connected-component flood fill). `cascade.py`'s `V = alpha·h^exp`
(`cascade.py:244-257`, duplicated verbatim at `:450-463`) fits `alpha` from a single
config `(FRL, V_FRL)` pair with `exp` defaulting to an unfitted 2.5. They are structurally
incompatible representations (grid-derived curve vs. closed-form power law), not just
disconnected by a missing function call — reconciling them means picking a canonical
representation and converting, not writing a bridge.

**`wse_m` for cascade scenarios is asserted from config, never checked against the DEM**
(`run_pipeline.py:414-417`, `wse_m = cascade_cfg["reservoir"]["z_crest_m"]`) — this is
the *same class of bug* memory.md already documents for Annamayya's DEM
(`floodsight-annamayya-dem-limit`), but structural: it will recur for every cascade
scenario until the reservoir initial condition is reconciled against terrain, not
patched per-scenario.

**Test coverage on the live path is effectively zero for physical invariants.** Mass
closure, monotonic drawdown, and breach-width monotonicity are asserted only inside
`test_event_graph.py` — the module nothing calls in production. The one test that
exists on `simulate_prebreach_rise` (`test_annamayya_cascade_arrivals.py:29-31`) checks
only that the final elevation lands near two hardcoded numbers, using a
`pre_breach_s=5400.0` that is **not** the production default
(`lead_time_s + 1800 = 12600s`) — the production trajectory is untested.

**`dam_type` is plumbed end-to-end (CLI → API → `DamGeometry`) and read by zero physics
code** — this is the exact and complete explanation for the one currently-failing test,
`test_ground_connectors.py::test_cwc_dam_engineering_override` (asserts concrete
formation time > earthfill; both come out identical because nothing branches on
`dam_type`). This test is a specification for what mechanism-awareness needs to satisfy,
not an unrelated pre-existing failure to route around — flagging the reclassification
since P1 checkpoints called it "pre-existing, unrelated, untouched." It is pre-existing,
but it is exactly what P2 fixes.

---

## 1. Design stance carried through every gate below

- **One canonical stage-storage representation per STORAGE node.** DEM-derived
  (`build_stage_storage`) is authoritative wherever the DEM supports a connected pool;
  the analytical power law survives only as a labeled, provenance-tagged fallback,
  mirroring the existing `except Exception` pattern at `run_pipeline.py:558-571` (per
  §O acceptance condition 2). When both exist, compute and store
  `reconciliation_gap_m3` rather than silently preferring one (per §O's
  `StageStorageCurve` schema) — never average or blend them.
- **One continuous state through breach**, not two integrators with a logged gap.
  Failure state (`INTACT | OVERTOPPING | BREACHING | BREACHED`) lives on the STORAGE/
  STRUCTURE pair and transitions are driven by the computed trigger, not by a second
  function starting cold at an asserted initial condition.
- **One breach-model interface, mechanism-selected**, not three independent kernels
  competing by accident of which function a caller reaches. Every mechanism that maps
  to an implementation must produce width, invert, discharge, and formation time from
  the *same* state-transition contract; every mechanism that doesn't map to a real
  implementation returns `NOT_IMPLEMENTED` from the API, never a silent fallback to the
  generic kernel.
- **Ensemble arms keep method identity.** Stop re-labeling by post-hoc discharge rank.
  If Von Thun cannot independently produce Qp, the ensemble must say so per-arm
  (`parameter_source`, `model_form_uncertainty` flag) rather than silently presenting
  three numbers as if from three independent methods.
- **Preserve validated behavior unless a documented physics correction is intentional.**
  Every gate below ships a regression test proving old scenarios still produce the same
  output *unless* the gate's own changelog entry says otherwise and why.
- **Indian scenarios are the implementation target; Derna is a benchmark only** — no
  geometry or parameter values cross from Derna into any Indian scenario at any gate.

---

## 2. Gates, in dependency order

Each gate names: what changes, which files, the acceptance test, and the explicit
decision points that need your sign-off before implementation starts on that gate (per
directive §22 — real decisions only, not routine engineering choices).

### Gate 1 — Physical geometry (foundation, mostly already correct)

Confirm per-scenario: DEM coverage, structure location, connected-pool feasibility.
`fill.build_stage_storage`'s confinement guard (`fill.py:154-159`) already fails loudly
when a pool touches the raster edge — keep this behavior, extend the same guard to run
for every scenario's STORAGE node before Gate 2, not only the non-cascade path.

**Known-blocked today:** Annamayya (`floodsight-annamayya-dem-limit` — dam not resolvable
in a 30 m DEM, no coordinate fix can change this) and South Lhonak's downstream
Chungthang node (a *different* structure at a *different* elevation than the DEM/AOI's
5200 m lake, sharing one scenario record, per the audit's `data_fetcher.py:298-377`
finding). Both stay `hydraulic_ready: false` for the affected node until surveyed
geometry exists — no coordinate juggling attempted.

**Decision point:** none — this gate only runs the existing guard more widely and
reports status; no new physics.

### Gate 2 — Stage-storage / lake formation

Build the canonical `StageStorageCurve` (§O schema: `source: DEM_FILL |
ANALYTICAL_POWER_LAW | SURVEYED`, `h[]/V[]/A[]`, `classification`,
`reconciliation_gap_m3`) per STORAGE node. Route `build_stage_storage`'s output into
the cascade path for every scenario where the DEM supports a connected pool — this
replaces `cascade.py:244-257`'s single-pair power law as the *primary* source; the power
law becomes the documented fallback, tagged `ANALYTICAL_POWER_LAW`, used only when the
DEM fill fails or the pool is unconfined.

Fix the duplicate block (`cascade.py:244-257` == `cascade.py:450-463`) by extracting one
`_stage_storage_for(cfg)` helper both `simulate_generic_cascade` and (its replacement in
Gate 4) call.

**Acceptance test:** monotonicity of V/A/depth vs. level on the `levels_m=` path
specifically — the audit found this path (the one cascade actually uses) has **zero**
coverage; only the `fractions=` path is tested (`test_lake_cascade_gee.py:45-50`, on a
synthetic cone DEM). New test must run on a real scenario DEM.

**Decision points:**
1. When DEM fill and the power-law fallback both produce a curve for the same STORAGE
   node (possible on scenarios where the pool is confined but a `cascade` config also
   exists, e.g. Annamayya), and they disagree beyond a threshold — is a large
   `reconciliation_gap_m3` a hard failure (`hydraulic_ready: false`) or a surfaced
   warning that still lets the run proceed? Recommend: surfaced warning with the gap
   number in the manifest, not a hard block — matches the existing FS-05 finding's
   framing ("closes FS-05's silence," §O) rather than adding a new gate that blocks
   Annamayya, which already has a known 12.5 m gap.
2. What threshold counts as "disagree"? No existing precedent in the codebase to anchor
   this — needs a number from you or an explicit placeholder pending domain input.

### Gate 3 — Failure trigger (computed, not asserted)

Replace `run_pipeline.py:414-417`'s asserted `wse_m = cascade_cfg["reservoir"]["z_crest_m"]`
and `cascade.py:277`'s asserted `v_res[0] = v_from_elev(z_crest + 0.05)` with a single
continuity integration that starts from the scenario's documented pre-event state
(FRL, or `z_bed` if no fill history exists) and runs forward under real inflow until
`level >= crest` (or the mechanism-specific criterion from Gate 4) is crossed —
adopting `event_graph.py:234`'s trigger logic (`stage >= overtopping_elevation_m`,
latched once) as the pattern, generalized off `event_graph`'s two-node-type restriction.

This is where `simulate_prebreach_rise` and `simulate_generic_cascade` merge into one
state machine (§0 above) — the biggest single code change in the plan, touching every
cascade scenario's numeric output.

**Acceptance test:** the trigger crossing time must be reproducible from the state
trace alone (no hardcoded `t=0`); regression test asserts the crossing time for
Annamayya lands within the uncertainty band already established this session
(P1-2's T=0 = 06:30 IST washout, `lead_time_s` = 10800) — if it doesn't, that's a
finding to report, not a discrepancy to tune away.

**Decision point:** Annamayya's own scenario record carries two different T=0 anchors
(`data_fetcher.py:392`'s `event_origin_ist` = 05:45 IST vs. the P1-2-reconciled event
clock's 06:30 IST washout — audit finding, not previously known). This needs resolving
*before* Gate 3 can pick what the integrator's target crossing time even is. Options:
(a) treat 06:30 as authoritative (matches P1-2's already-completed reconciliation
against the evidence dossier) and correct `event_origin_ist` to match, or (b) treat
`event_origin_ist` as a separate, distinct anchor (e.g. "first report" vs. "washout")
and keep both with explicit labels. Recommend (a) for consistency with work already
committed this session, but this is squarely a "historical sources conflict materially"
case per directive §22 — flagging rather than deciding.

### Gate 4 — Failure mechanism (the taxonomy + interface)

Implement the `FailureModel` interface from directive §8
(`trigger()/initialize()/update(dt, lake_state)/breach_geometry()/discharge()/completion_state()`)
and attach `failure_mechanism` to geometry features via `scenarios.py`'s `valid_roles`
(the audit confirms `role`+`classification`+`source` are *already* enforced per-feature
at `scenarios.py:93-94` — this is a real, already-load-bearing home, not a stretch).

Per §P's status table, implement genuinely distinct parameter/behavior paths for the
three mechanisms the directive marks as minimum-viable:
- **Overtopping erosion** — closest to existing code; reconcile `cascade.py`'s
  rectangular weir against `ensemble.route_breach`'s trapezoidal form and pick one
  (see Decision point 1).
- **Progressive embankment breach** — currently two disagreeing, uncited exponents
  (`cascade.py`'s `^1.2`/`^0.8` vs. `event_graph.py`'s linear ramp). Neither has a
  citation. Needs either a literature source for one of the two, or an explicit
  `ASSUMED` classification with the exponents named as calibration knobs, not physics.
- **Piping / internal erosion** — currently `NOT_IMPLEMENTED` in truth (the API accepts
  `failure_mode="piping"` but it produces physics identical to any other value). This
  is new implementation, not a fix — needs a real seepage/internal-erosion formulation
  (e.g. a piping-erosion time-to-breach model from the dam-safety literature) or an
  honest `NOT_IMPLEMENTED` response.

Every other mechanism in the directive's list (sudden/rapid, partial, foundation,
structural/concrete, gate failure, spillway failure, landslide-induced overtopping,
earthquake-induced, natural landslide-dam, moraine/GLOF, river-blockage outburst) gets
a named `failure_mechanism` enum value and an explicit `NOT_IMPLEMENTED` return — no
code changes to physics for these beyond making the taxonomy exist and the honesty
mechanism work.

Fix `test_ground_connectors.py::test_cwc_dam_engineering_override` as part of this gate
by making `dam_type` actually reach `formation_time_h` (per audit: it currently reaches
nothing) — this is the test's whole point, not a side effect.

**Acceptance test:** unsupported-mechanism → `NOT_IMPLEMENTED` from the API (new test);
`test_cwc_dam_engineering_override` passes for a real reason (concrete formation time
genuinely differs, not coincidentally).

**Decision points:**
1. **Kernel reconciliation for overtopping erosion**, per your directive item 2 — this
   is the term-by-term comparison you asked for, summarized: `cascade.py`'s kernel is
   the only one every current validated run and archived output was produced with, but
   it (a) mislabels itself as trapezoidal when it's rectangular, (b) has uncited
   exponents, (c) has no mass-availability bound. `ensemble.route_breach`'s kernel has
   a real trapezoidal term and is closer to NWS DAMBRK convention (cited constants,
   `ensemble.py:114-116`) but has a different, also-uncited linear invert-erosion law
   and has never been run against any cascade scenario. `event_graph.py`'s kernel has
   the only real trigger and the only mass bound but no invert erosion and is linear
   in width. None of the three is simply correct. Recommend: build the new mechanism's
   kernel using `ensemble.route_breach`'s trapezoidal discharge form (cited weir
   constants) + `event_graph.py`'s mass-bound check, with the erosion-rate exponent
   left as an explicitly-labeled `ASSUMED` calibration parameter (not claimed as
   physics) until a citable source is found — this reproduces no single existing
   kernel exactly, so it requires new regression baselines for every cascade scenario,
   which is real re-validation work, not free. Alternative: keep `cascade.py`'s kernel
   verbatim (bugs and all) for the `overtopping_erosion` mechanism specifically, so
   zero existing scenario output changes, and reserve the new interface for mechanisms
   that don't have live output to preserve (piping, first). This is the actual
   trade-off — bring physical correctness in line with the interface now (re-validate
   everything), or preserve continuity for the mechanism that's live and build the new
   interface's rigor into new mechanisms only. Needs your call.
2. **Piping/internal-erosion model choice** — no existing code fragment to build from;
   this is genuinely new. Needs either a named source (e.g. Fread's BREACH model, or a
   simpler time-to-failure regression from the dam-safety literature) or an explicit
   decision to leave piping `NOT_IMPLEMENTED` for this pass and scope it as a later
   gate. Recommend leaving it `NOT_IMPLEMENTED` initially — the directive's own minimum
   list only requires the *taxonomy and honesty mechanism* to exist for all 14
   mechanisms; only 2-3 need real physics this pass, and overtopping + progressive
   embankment are the ones with live scenarios depending on them.

### Gate 5 — Breach hydrograph (storage-consistent)

Enforce, in the new unified integrator from Gate 3: no negative storage, monotonic
drainage where the mechanism implies it, mass closure (input = output + Δstorage,
asserted, not just computed-and-ignored — the audit found `cascade.py`'s
`mass_error_pct` is computed but never asserted anywhere), and `discharge ≤
available_water / dt` (the check `event_graph.py` already has and `cascade.py` lacks).

**Acceptance test:** the mass-closure assertion pattern already exists and passes in
`test_event_graph.py:18-19,39-40` — port the same assertion onto the new unified
integrator's output for every cascade scenario. Add: wider breach → higher peak
discharge, monotonically, for the mechanism(s) implemented in Gate 4 (audit found no
such test exists anywhere; the closest, `test_ritter.py:90-95`, only asserts the
ensemble sort order is self-consistent with itself, which is tautological).

**Decision points:** none beyond what Gate 4 already resolves — this gate is
enforcement of invariants on whatever kernel Gate 4 selects, not a new design choice.

### Gate 6 — Event graph execution (migration, last for physics)

Only after Gates 2-5 land: extend `event_graph.py`'s node-type set from
`{"forcing", "reservoir"}` (`event_graph.py:57`) to the full `SOURCE / STORAGE /
STRUCTURE / REACH / JUNCTION / SINK` taxonomy (§H), rewrite `cascade_config_to_graph`
correctly this time (fixing the ~2900× alpha error and the missing
`invert_elevation_m`/`overtopping_elevation_m` the audit found), and route
`run_pipeline` through `simulate_event_graph`. Delete `simulate_annamayya_cascade`
(`cascade.py:526-579`, confirmed stale — `lead_time_s: 9000.0` vs. the live
`10800.0`) and its inline duplicate config at the same time.

Split South Lhonak into two STRUCTURE nodes (moraine/upstream, Chungthang/downstream)
per FS-43 — each with its own `failure_mechanism`, `trigger`, and breach parameters,
resolving the audit's finding that these are currently two physically distinct
structures at two different elevations (moraine lake ~5200 m vs. Chungthang ~1100 m)
sharing one scenario record and one breach kernel.

**Acceptance test:** for every currently-executable cascade scenario, the migrated
graph path's output is compared against the pre-migration output and any difference is
either (a) zero, proving the migration preserved behavior, or (b) attributed to a
specific, named physics correction from Gates 2-5, with the old and new numbers both
recorded in `findings_results.md`. No silent behavior change is acceptable per the
directive's own item 9 requirement.

**Decision point:** none new — this gate is "finish what §H proposed," gated on Gates
2-5 having already fixed what made the original migration wrong (the numerical bug in
`cascade_config_to_graph`, the missing mechanism selection). The only new judgment call
is scheduling: this is explicitly the *last* physics gate, run only once 2-5 are stable
and re-validated, per your directive's "do not migrate prematurely."

### Gate 7 — 2-D multi-inflow routing

Extend `run_2d_swe_simulation`'s signature from scalar `inflow_x_idx/inflow_y_idx` +
one hydrograph to a list of `{x_idx, y_idx, hydrograph_t_s, hydrograph_Q_m3s, source,
classification}` sources (directive §12's exact shape). Per the audit's precise
description of the current mechanism (`swe_2d.py:671-679` kernel build,
`swe_2d.py:721-728` injection): the per-step injection loop generalizes
straightforwardly to sum over sources, but the active-window optimization
(`_active_window`, seeded on the single inflow point, `swe_2d.py:697/704`) is the real
work — it currently assumes one growing blob and must become either a union bounding
box over all active sources or a per-source window merge. This is exactly the kind of
solver-adjacent change the directive says to make without "rewriting the validated
physics" — the Rusanov/MUSCL/RK2 core is untouched; only the window-seeding and
injection-summation logic changes.

Also note (audit finding, not previously flagged): the injection currently adds mass
with **zero momentum** despite a comment claiming otherwise (`swe_2d.py:721`,
"and the momentum that mass arrives with" — `hu`/`hv` are untouched). Not in scope to
fix as physics correctness (out of bounds per the "preserve solver" instruction), but
worth flagging since multi-inflow will make this approximation apply at N points
instead of 1, and a fast second inflow entering with zero initial velocity may look
visibly wrong on a narrow channel. Recommend leaving as-is for this pass, revisit only
if it produces a visible artifact on a real multi-inflow scenario.

**Acceptance test:** `tests/test_swe_active_window.py` (memory.md: compares windowed
vs. full-domain output bit-for-bit) must still pass unmodified for every existing
single-inflow scenario; new test adds two simultaneous sources on a synthetic domain
and asserts total injected volume equals `Σ(∫Q_i dt)` exactly, and that the windowed
path still matches a full-domain solve bit-for-bit with two sources.

**Decision point:** none — this is a contained, well-specified extension once Gate 6's
graph produces multiple SINK nodes to feed it.

### Gate 8 — Consequences

No new design work — P1-3/P1-4 already fixed the consequence-chain bugs (FS-23/24/27/28)
and provenance propagation (FS-11/25/29/30/50) this session. This gate is: re-run the
consequence chain against Gate 6's graph-based output once it exists, confirming
`isolation.py`'s per-timestep safe-node logic and `exposure.py`'s `MASK_FAILED` flag
still behave correctly when depth rasters originate from a multi-inflow solve instead
of a single central-arm solve. No new acceptance criteria beyond "P1-3/P1-4's existing
tests still pass against the new pipeline."

### Gate 9 — Historical validation (Kosi 2008)

Per your directive, explicitly gated behind Gates 1-6 being stable (§O acceptance
condition 1: "every executable scenario reaches an inflow→storage→level→outflow
integrator before breach" must hold for Kosi before its output means anything).

Steps, in the order the directive specifies (§18): source-verify
`data/validation/gfd_dam/DFO_3382_From_20080922_to_20080929.tif` (confirm it actually
covers the Kosi breach location and event window — not yet verified this session, only
confirmed to exist on disk per the earlier review pass), build the canonical 20-field
record (§2's requirement, referenced in the review doc's §F), create an
`observation_manifest` record for it (per §G's gating rule — "no comparison should run
against a dataset without a manifest record"), verify CRS/AOI, then and only then
attempt a validation score.

**Decision point:** Kosi's breach location, embankment geometry, and pre-event
hydrology are not yet sourced in this repository (`data_fetcher.py` has only a stub,
per the original review's §F table: `INDIAN_EVENT_CATALOG["kosi_2008"]`, "pending
source registration"). Building the scenario record is itself a research task — sourcing
historical embankment height/length, breach width/timing from published post-event
assessments (the 2008 Kosi breach is well-studied; CWC and independent academic
sources exist). This needs either your sign-off to spend a research pass sourcing it,
or a specific source you already have in mind, before Gate 9 can start. Flagging per
directive §22 ("deciding whether a missing quantity can be reconstructed").

### Gate 10 — Performance

Explicitly last, per your directive (§19 for the analogous UI gate, and the general
"do not optimize for every scenario runs" framing) — no performance work competes with
correctness here. The existing P3 backlog (FS-33–36, frame streaming, raster-first
playback, road tiling) stays deferred until Gates 1-9 are stable. Not re-scoped by this
plan.

---

## 3. What this plan deliberately does not do

- **No historical comparison UI** (old P2-3) until Gate 9 produces a second real
  validated scenario to compare against — building the panel first would give it
  nothing honest to show.
- **No Kashmir/Assam/Wapriyang scenario work** — these need rainfall-runoff forcing
  FloodSight doesn't have "by design" per `memory.md`'s Derna scope-ceiling precedent;
  that's a product-scope decision, not something this plan resolves by building
  physics around it.
- **No solver rewrite.** Audusse/MUSCL/RK2/Rusanov/friction/active-window stay exactly
  as validated. Gate 7 touches only the injection and window-seeding logic.
- **No blending of DEM-derived and analytical stage-storage curves.** They're
  reconciled (gap reported) or one is chosen per Gate 2's fallback rule — never
  averaged.
- **No mechanism gets fake physics to avoid saying `NOT_IMPLEMENTED`.** Per directive
  §7 and §23 — the honest-absence path is a feature of this plan, not a shortfall.

---

## 4. Decision points — resolved 2026-09-11

All five were put to the user and answered; recorded here so implementation doesn't
re-litigate them.

1. **Gate 2** — DEM-fill vs. analytical-power-law disagreement: **surfaced warning,
   run proceeds** (not a hard failure). Report `reconciliation_gap_m3` in the manifest,
   matching the existing FS-05 framing. Threshold for "disagree" still has no numeric
   precedent in the codebase — pick one when Gate 2 is implemented and record the
   reasoning in `findings_results.md`; it is not being fixed in advance of real data
   because there's nothing to calibrate it against yet.
2. **Gate 3** — Annamayya's two T=0 anchors: **correct `event_origin_ist` to 06:30
   IST**, matching P1-2's reconciliation against the full evidence dossier. One anchor
   per scenario. `data_fetcher.py:392` needs this edit as part of Gate 3.
3. **Gate 4** — Overtopping-erosion kernel: **rebuild on `route_breach`'s trapezoidal
   form + `event_graph`'s mass-availability bound.** This is a deliberate,
   acknowledged behavior change — every cascade scenario's breach hydrograph shape
   will differ from today's output. New regression baselines and re-validation for
   Annamayya and South Lhonak are required, not optional cleanup; record old vs. new
   numbers in `findings_results.md` when Gate 4 lands, per §0's "no silent behavior
   change" rule.
4. **Gate 4** — Piping/internal erosion: **defer, mark `NOT_IMPLEMENTED` honestly.**
   No source model chosen; no scenario depends on it. Revisit as its own gate once a
   named source (e.g. Fread's BREACH model) is picked.
5. **Gate 9** — Kosi 2008 sourcing: **approved — dispatch a research pass** to find
   citable sources (CWC / academic) for the 2008 Kusaha embankment breach before
   writing any scenario file. Research report comes back for review before the
   scenario record is built. **COMPLETED 2026-09-11 — see §5 below; it invalidated
   this plan's own premise for Gate 9.**

---

## 5. Gate 9 research result — the raster premise was false (2026-09-11)

The Kosi research pass came back and **falsified the stated reason Kosi was chosen as
the first new validation candidate.** Recorded here rather than quietly corrected,
because two repo documents assert the false premise.

**The raster on disk is the wrong event.** Verified independently against
`data/validation/gfd_meta/dam_events.csv` (not taken on the researcher's word):

```
ID 3382 | India | long 84.406372 | lat 20.95224
Began 2008-09-22 | Ended 2008-09-29 | MainCause "Dam release and Heavy Rain"
```

`20.95 N, 84.41 E` is **Odisha — roughly 700 km south-southwest of the Kusaha breach**
(~26.6 N, 87.05 E), starting **35 days after** it, from a dam release. This is almost
certainly the September 2008 Mahanadi/Hirakud event. It is not Kosi.

**What asserts otherwise, and must be corrected when Gate 9 runs:**
- `FLOODSIGHT_DEEP_REVIEW_2026-09-11.md:321` — Kosi row states
  `DFO_3382_...tif` "covers the 2008 window and is **already on disk**", and this is
  the entire stated basis for "Kosi 2008 is the shortest path to a second validated
  case" (also repeated at `:328` and in the P2-2 line at `:626`).
- `data/validation/README.md:51` — lists 3382 as the India 2008 event with no
  indication it is Odisha.

**The trap, measured.** The GFD regional MODIS tile for 3382 spans 72–91 E, so it
*does* geographically overlap Bihar, and within a Kosi-fan box it carries ~28,000
flooded pixels. A CSI/POD/FAR score run against this file would therefore **return
plausible-looking numbers from the wrong flood on the wrong date** rather than failing
loudly. This is exactly the failure mode §G's provenance gate exists to prevent, and
it would have passed a naive "the raster exists, score against it" check.

**The correct event exists but is NOT on disk.** `gfd_available_events.csv:511`:

```
ID 3365 | Nepal | long 84.923215 | lat 26.950613
Began 2008-08-18 (exact breach date) | Ended 2008-09-24
DFO_3365_From_20080818_to_20080924.tif | 32.47 MB
```

Two caveats before it is trusted: its `MainCause` is "Heavy monsoon rains", **not** a
dam/embankment cause — which is precisely why the repo's dam-event filter excluded it
and it was never downloaded — and its window is a **37-day MODIS maximum-extent
composite**, not a snapshot, so it includes recessional extent and will bias any
extent-based score.

**Consequence for the plan:** Gate 9 is still viable but is **no longer "the shortest
path"** — it requires a 32 MB download, an `observation_manifest` record, and a
correction to the two documents above. It should not be described as low-cost again
without that work being counted.

### What the research did establish, and at what confidence

Usable (HIGH, peer-reviewed — Sinha et al. 2013 *Geology* 41(10):1099; Sinha et al.
2014 *Geomorphology* 216:157-170; GFDRR/World Bank Bihar Kosi Needs Assessment):

- **Date** 18 August 2008; breach plugged January 2009.
- **Location, relative only** — eastern embankment, 12-13 km upstream of the Kosi
  Barrage (26.5263 N, 86.9269 E).
- **Failure mechanism, well documented and explicitly not overtopping** — lateral
  thalweg migration against the eastern embankment since 2000, spur erosion driving
  toe erosion and seepage from both faces, on a channel whose bed sits ~4 m *above*
  the adjacent floodplain. Sinha 2014 states directly that the avulsion "was not
  caused by a large flood event."
- **Breach discharge ~4078-4320 m3/s against a ~27,000 m3/s design capacity** — i.e.
  roughly **15% of design and 40% of bankfull** (bankfull 7458 m3/s, mean annual flood
  9183 m3/s at Birpur). Any scenario driving this with a monsoon-peak hydrograph is
  physically wrong.
- **Surveyed embankment crest elevations** at three reaches (Sinha 2014 Table 4),
  giving derived embankment heights of 4.67-6.94 m above floodplain.
- **Published breach width 1500 m** (UNITAR 2008 via Sinha 2013).
- **Impact** 2722 km2 avulsion belt / ~3700 km2 inundated in Bihar; 3.3 M affected;
  993 villages.

Must stay unresolved rather than be filled in:

- **No published breach lat/lon anywhere.** Best available is a village centroid, with
  two similarly-named candidates (Paschim Kasuha vs Purbakushaha) ~20 km apart, only
  one of which is consistent with "12 km upstream on the eastern embankment". Do not
  present a derived coordinate as sourced.
- **Discharge spans 3675-4320 m3/s**, including a disagreement between two papers by
  the same author (4078 vs 4320).
- **Breach width 1500 m vs ">2 km"** — possibly different times, no source says so.
- **Deaths 250-527; displaced 3 M vs 10 M.**
- **Not found at all:** crest width, pre-breach discharge time series, documented
  breach time of day.

Adding a real Kosi scenario touches ~23 registry locations per the deep review's §K
count. `src/scenarios.py:52` currently holds the only executable reference:
`"kosi_2008": {"name": "Kosi flood, 2008", "description": "Indian event record pending
source registration"}`.

Everything else in the 10 gates is routine engineering once these are settled, per the
directive's own distinction in §22.
