# FloodSight implementation blueprint

Baseline is recorded in FLOODSIGHT_IMPLEMENTATION_PLAN.md. Preserve the existing dirty worktree. Source/observation work is assigned separately. Implementation is authorized; no further plan approval is needed. These are engineering instructions, not claims that the historical inputs have been verified.

## Physics implementation

ARCHITECT BLUEPRINT FOR LUNA

Target File: D:\sih_work\floodsight\src\m3_breach\event_graph.py (new).

Exact Location: New shared hydrologic integration module.

Objective: A conservative directed acyclic event graph for single, sequential, parallel, multiple-upstream, rainfall and reservoir cascade configurations.

Scope: Lumped hydrology and level-pool reconstruction. Preserve the finite-volume solver; do not claim debris transport, dynamic reservoir waves or operational calibration.

Contracts: simulate_event_graph(graph, time_edges_s, breach_tier="central") is synchronous and returns EventGraphResult. The finite, strictly increasing time grid has N edges. Each transfer carries N−1 interval-mean discharges; each storage/stage series has N edge values. Results expose node_results, edge_results, terminal_outflows, events and mass ledgers, with a JSON-safe serialization method. Nodes require unique id, type, configuration and source/classification; geometry_id is optional to the hydrology but required before spatial coupling. Supported types are forcing and reservoir. Edges require unique id, source, target and explicit routing configuration. Reject cycles, dangling references, unsupported methods, nonfinite/invalid parameters and missing required source classifications. Reservoir configuration contains explicit initial storage, monotone stage-storage or an explicitly reconstructed power-law curve, separate spillway and breach parameters, and either a classified scheduled trigger or a simulated overtopping threshold. Missing observations must never become numerical forcing.

Micro-Steps for 5.6 Luna:
1. Validate and topologically order the graph using the standard library. Evaluate constant, finite pulse, Gaussian or supplied sampled forcings on the shared interval grid. Separate background river flow from finite upstream reservoir-release volume; normalize a release only when an explicit release volume is provided.
2. Route each edge and retain the change in channel storage in its ledger. Sum incoming transfers before integrating a reservoir. Do not force finite-window routed volume to equal input while a tail remains in the reach.
3. Integrate each reservoir once from the earliest event time, including negative pre-breach times. Use a monotone implicit continuity solve with SciPy bracketing, or equally justified conservative bounded fluxes, so storage cannot become negative. Never clip negative final storage while retaining the original discharge.
4. Compute nonnegative spillway head, rating/capacity and controlled-target rule separately from breach discharge. A gate cannot discharge water below its hydraulic crest. Compute width/invert from elapsed time after the actual scheduled or overtopping trigger; use the same equation throughout, including the final interval.
5. Emit stage, storage, inflow, spillway, breach, total outlet, width/invert and derived phase events. Audit the exact interval fluxes used by continuity; reject residual greater than 1e-8 of available volume, with a small absolute tolerance for a dry system.

Verification: Add tests for dry/no-source state, constant-flow conservation, aggressive drainage, below-crest spillway, trigger timing, parallel addition, two reservoirs in series, routing tail storage, cycle/missing-source rejection and nonfinite input. Test hypothetical natural-blockage/GLOF storage nodes without claiming sediment physics. Test each existing cascade configuration without tuning it to force historical agreement.

Assumptions/Blockers: Existing power-law geometry and forcing are reconstructions. Numerical validity does not certify historical terrain/storage.

Target File: D:\sih_work\floodsight\src\m3_breach\cascade.py.

Exact Location: CascadeResult, PrebreachResult, route_muskingum_1d, simulate_generic_cascade, simulate_prebreach_rise and simulate_annamayya_cascade.

Objective: Replace duplicate pre/post integrators with views of one graph trajectory.

Scope: Preserve callable legacy signatures/aliases. Remove the duplicate Annamayya fallback dictionary.

Contracts: cascade_config_to_graph(cfg, breach_tier="central") translates the existing cascade dictionary into the shared graph. Existing results expose explicit full trajectory/time edges/interval fluxes in addition to compatibility views. Pre-breach and post-breach default views must refer to the same initial-value problem and exactly share the state at zero. Do not disguise interval means as linear point samples. The legacy sampled Muskingum helper retains a sampled-series return and validates a uniform time grid.

Micro-Steps for 5.6 Luna:
1. Translate upstream pulse plus routed edge and catchment forcing into one reservoir node. Start at minus lead time minus 1800 seconds using explicit FRL storage. Old scheduled zero-time failure is a RECONSTRUCTED assumption unless stronger evidence is registered; do not call it observed overtopping.
2. Use stable Muskingum subreaches satisfying dt >= 2*K_sub*X and dt <= 2*K_sub*(1-X), with K positive and X between zero and one-half. Record the selected count/effective settings. Reject unsupported time discretization; never hide instability by clipping negative flow. USACE recommends estimating subreaches from K/dt: https://www.hec.usace.army.mil/confluence/hmsdocs/hmsguides/applying-reach-routing-methods-within-hec-hms/applying-the-muskingum-routing-method .
3. Remove crest-plus-0.05 initialization, the special final breach sample and the n-sample/n−1-step mass mismatch. Use the graph's separated outlet terms and exact interval audit.
4. Make simulate_annamayya_cascade perform canonical lookup and call the generic adapter; missing configuration raises clearly.

Verification: Reproduce then eliminate the Annamayya 7.5743656 MCM phase jump, Derna negative-head NaN, and terminal breach spike at 300 seconds. A 10000 m³/s pulse lasting 600 seconds must not create the old 20.12 MCM routed volume from 6 MCM input. Check shared state equality, nonnegative finite storage and complete reach/reservoir ledgers. Run python -m pytest -q tests/test_annamayya_cascade_arrivals.py tests/test_lake_cascade_gee.py after replacing obsolete physics expectations with invariants.

Target File: D:\sih_work\floodsight\src\m3_breach\ensemble.py and, only if needed, D:\sih_work\floodsight\src\m3_breach\__init__.py.

Exact Location: Hydrograph, build_ensemble, route_breach, get_hydrographs and DamGeometry.

Objective: Reuse conservative reservoir continuity for single-dam arms and preserve method identity.

Scope: Keep empirical formulas as estimates; remove unsupported concrete/masonry multipliers and spillway addition to empirical breach peak.

Contracts: Hydrograph retains old fields and may add time_edges_s, interval_Q_m3s, component discharges and mass audit. Supplied stage curves must be finite and ordered/monotone; reject malformed curves rather than sorting them into apparent validity. Rated spillway capacity alone is insufficient for a stage-discharge curve; expose missing rating rather than invent one.

Micro-Steps for 5.6 Luna:
1. Translate a single-dam arm into the shared reservoir model using the same stage-volume curve for initial state and depletion.
2. Remove the empirical spillway peak addition. Compute separated spillway discharge only from a complete explicit rating configuration.
3. Preserve actual method IDs when sorting routed peaks into optimistic/central/pessimistic labels; central is not necessarily Froehlich.
4. Keep triangular hydrographs only as explicitly selected comparison/reconstruction data and terminate their finite support. Never extrapolate a nonzero tail forever.

Verification: H=100 m, V=1000 m³, B=10000 m, dt=30 seconds cannot release more than storage plus inflow. Check partial-fill curve consistency, invalid curves, finite tail and arm ordering/method identity.

Target File: D:\sih_work\floodsight\src\m4_solvers\swe_2d.py and D:\sih_work\floodsight\src\m4_solvers\swe_2d_gpu.py.

Exact Location: run_2d_swe_simulation, GPU equivalent, source preparation, active-window bounds, time stepping, save/arrival and mass audit.

Objective: Exact conservative spatial source coupling and correct solver timestamps/accounting.

Scope: Preserve Rusanov/MUSCL/SSP-RK2. Level-pool reservoir storage remains outside the 2D domain; never initialize the same stored volume in 2D and also inject its release.

Contracts: Optional sources is a list of dictionaries carrying id, time_edges_s, interval_Q_m3s, normalized interior raster weights and declared velocity_xy_mps. M2 supplies validated opening geometry and weights. Reject empty/nonfinite/negative/boundary source weights, invalid grids, nonfinite Q and negative depth. Optional cancel_cb can return true or raise; cancellation propagates. Legacy point-source signatures remain for idealized numerical tests, explicitly identified as such; the production pipeline requires validated physical sources. Unsupported geometric-source GPU execution chooses the validated CPU path before execution and records the actual backend, or fails explicitly.

Micro-Steps for 5.6 Luna:
1. Include all source cells in the active window; a conservative full-domain fallback for multiple sources is acceptable initially.
2. Integrate source volume exactly over each SWE step, including source interval boundaries. Add momentum only from declared inlet velocity. Legacy sampled hydrographs use exact piecewise-linear integration with zero outside finite support.
3. Save the exact initial state at zero and exact requested/final times. Limit dt by CFL, next save/source boundary and final time; remove the minimum dt that can violate these bounds. Fail on unusable/nonfinite dt.
4. Record arrival for the computed state at t+dt. Weight first RK-stage positivity correction by one-half in the final ledger; retain separate boundary, correction, initial, injected and stored terms.
5. Check cancellation explicitly outside swallowed progress-report exceptions.

Verification: Run existing SWE validation, active-window, GPU and Ritter suites. Add exact injected-volume/tail tests across irregular steps, two spatial sources, dry-no-source, initial/final/arrival timing, invalid support, cancellation and clipping-ledger closure. Do not claim unavailable GPU hardware was tested.

Target File: D:\sih_work\floodsight\src\m4_solvers\sph_swe.py.

Exact Location: run_swe_sph_1d, SPHResult.sample_on and run_scenario_thalweg_sph.

Objective: No spontaneous dry-particle mass and no misleading scenario comparison.

Scope: Preserve the real flat-bed Ritter research benchmark. Quarantine the scenario adapter until forcing, spatial support and comparison times are comparable.

Micro-Steps for 5.6 Luna:
1. Validate finite uniform spacing/nonnegative depth. Exclude dry particles from water mass. If equal-mass assumptions cannot support nonuniform wet depth/spacing, reject explicitly. Support an all-dry result without creating water.
2. Preserve the wet-only Ritter benchmark and sampling semantics.
3. Return available false and status NOT_AVAILABLE from the current scenario comparator, with explicit unequal forcing/domain/time reasons. Remove fabricated peak/RMSE/Delft3D claims, preserving interface keys needed for graceful unavailability.
4. Update only physics/SPH assertions in test_annamayya_cascade_arrivals.py; the observation editor owns its arrival test. Replace obsolete fixed-stage assertions with continuity/finite-state tests.

Verification: Five wet 1 m particles plus five dry particles at unit spacing contain 5 units of water, not 10. All-dry remains dry. Ritter remains valid. Unsupported scenario comparison never returns authoritative metrics.

## Integration constraints

The source/observation editor owns src/scenarios.py, src/data_fetcher.py, src/observation_manifest.py, src/gee_satellite.py and M10. Other changes must await those contracts. The pipeline must consume total spillway plus breach downstream transfer with source identity, use the actual outlet node's geometry (Al-Bilad versus Abu Mansour; Chungthang versus South Lhonak), and generate all reservoir stages and event times from the shared graph. A physical geometry gate must fail on missing dam/blockage shape, river connectivity, inconsistent datum/storage or unconfined pool. Existing missing geometry cannot be made verified by moving frontend coordinates into a backend file.

## Geometry, run integrity and pipeline implementation

ARCHITECT BLUEPRINT FOR LUNA

Target File: D:\sih_work\floodsight\src\m2_geometry\validation.py (new).

Exact Location: New canonical geometry validation and raster source preparation module.

Objective: Validate one physical structure, upstream impoundment and downstream outlet before producing hydraulic results.

Scope: Use canonical GeoJSON/source metadata. Never manufacture a dam axis, circular blockage, upstream/downstream vector or historical position. Permit explicitly classified analyst hypothetical geometry through the same checks.

Contracts: validate_geometry(manifest, elevation, transform, crs) returns a JSON-safe validation report plus prepared arrays/geometries, or raises a descriptive GeometryValidationError containing failed checks. Canonical geometry FeatureCollection roles are dam_body or blockage (Polygon/MultiPolygon), dam_axis (optional engineered crest line), breach_zone (Polygon/MultiPolygon), breach_point (Point), upstream_seed and downstream_seed (Points), river (LineString/MultiLineString), optional reservoir and spillway. Require source/classification for geometry and explicit vertical datum compatibility. Prepared output contains canonical projected and WGS84 coordinates, containing row/column, barrier mask, breach mask, connected upstream basin mask, downstream source weights and normalized downstream direction. All required geometries must lie within valid DEM coverage. A longitudinal river path is not a dam crest axis.

Micro-Steps for 5.6 Luna:
1. Validate finite coordinates, geometry validity/type/roles, projected metre units, north-up invertible affine and source identity. Use always_xy transformations. Verify round-trip errors and index-to-cell-center distance within half a cell diagonal; reject off-grid or nodata locations rather than clipping.
2. Rasterize the actual body/blockage and breach. Require breach intersection with the structure and river; use nearest point on river segments within an explicit maximum tolerance, never global lowest terrain. Persist original and accepted coordinates/displacement and reject ambiguous disconnected candidates.
3. Validate river topology through upstream seed, structure/breach and downstream seed. Verify seeds are on opposite sides of the barrier and downstream path is connected; use raster connected components on valid cells below declared water level with the actual barrier installed. Reject missing/ambiguous data, a filled pool reaching a domain/nodata edge, or a reservoir including the downstream seed.
4. Remove the breach opening from the barrier only for downstream coupling. Place source weights on the physically represented downstream opening; reject an opening unresolved at chosen resolution instead of spreading a Gaussian or clipping into a cell.
5. Compare computed impoundment volume and the declared stage-storage curve at matching elevations, with explicit documented tolerance/uncertainty. Do not use a fallback volume to erase inconsistency. Report DSM and vertical-datum limitations separately from horizontal topology.
6. Keep level-pool storage external to the 2D model, masking the reservoir side from injected water to prevent backfilling/double storage. Record that one-way level-pool coupling omits backwater feedback. Only supply inlet momentum when derived from declared hydraulic geometry/law; label any critical-flow assumption as reconstruction.

Verification: Synthetic metric valley fixtures test round trips, containing-cell indexing, actual polygon/axis crossing, wrong river/same-side seeds, off-grid/nodata, unconfined lake, disconnected opening, unresolved coarsening, volume mismatch and valid hypothetical opening. Historical Annamayya/Phutkal preflight must fail with precise reasons while verified geometry is absent; a coordinate candidate is not a passing test.

Target File: D:\sih_work\floodsight\src\m2_geometry\fill.py and D:\sih_work\floodsight\src\m2_geometry\dem_utils.py.

Exact Location: build_stage_storage, compute_lake_depth_grids, prepare_custom_dem, condition_gorge_thalweg.

Objective: Common physically confined storage/lake calculations with correct units/indexing.

Scope: Keep useful seeded-fill logic. Production callers must use actual rasterized barrier geometry. Legacy circular arguments, if retained for compatibility, must be explicitly hypothetical and never used by the pipeline.

Contracts: Fill functions accept an optional same-grid barrier_mask plus explicit barrier_crest_m and explicit levels_m. Lake frames require modeled absolute levels; arbitrary default fractions cannot create a historical time trajectory. Invalid seeds, shapes, nodata/edge escape or nonmonotone stage-volume relationships raise. Custom projected DEMs must use metre units; absent paths never silently fall back.

Micro-Steps for 5.6 Luna:
1. Share the barrier installation and confinement logic between stage-storage and lake frames. Do not clamp seeds. Include the same nodata/coverage validation in both.
2. Build the stage curve from the connected reservoir geometry at each physical level; represent insufficient low-stage connection explicitly instead of inventing gradual filling.
3. Require explicit absolute levels for historical lake frames and return volume/stage/area with classification. If a compatibility fraction API remains, call it stage_fraction rather than capacity and never attach fabricated timestamps.
4. Validate projected units and raster indexing; use rasterio rowcol containing-cell semantics in gorge conditioning. Do not apply scenario-specific channel burning outside canonical geometry.

Verification: Test confined/unconfined pools, out-of-range seed, nodata leaks, correct storage monotonicity and metre-versus-foot CRS rejection. Preserve existing valid custom DEM tests.

Target File: D:\sih_work\floodsight\src\run_manifest.py (new).

Exact Location: New shared durable manifest and artifact resolver.

Objective: Reproducible, valid-only run identity and artifact access.

Scope: Standard library plus installed Pydantic. No invented signing/key infrastructure, no destructive migration of old results.

Contracts: A version-1 manifest contains run_id, scenario_key, resolved scenario, request/config/model/input hashes, deterministic fingerprint separate from unique execution ID, created/started/completed UTC timestamps, status (queued/running/completed/failed/cancelled/interrupted), component states, validity with explicit geometry/physics/source checks and reasons, event_clock, timeline_events, consequence_scope, metrics, and artifact registry. Artifact entries contain confined relative path, SHA-256, byte size, media type and provenance. Helpers create/load/write atomically, transition allowed statuses, register artifacts, resolve a registered artifact, determine validity and choose latest completed valid run by completed timestamp then run ID. Unknown/malformed/incomplete manifests never qualify.

Micro-Steps for 5.6 Luna:
1. Serialize strict JSON without NaN, hash canonical resolved configuration and all model source files plus actual inputs, and atomically replace manifest files. Preserve unique executions even for identical fingerprints.
2. Enforce safe run IDs and paths under the run root, including symlink/resolved-path checks. Reject absolute paths or parent traversal in artifact entries. Register optional artifact failures as unavailable component states.
3. Valid completion requires passing geometry/physics checks and required artifacts, not merely a results file or a requested valid flag. Validate required artifact content hashes; selected optional artifacts also validate before serving. Cache verified hashes only against an appropriate file-stat identity and invalidate changed files.
4. Latest selection uses manifest validity and completed UTC time only. Ignore directory name, mtime, frame count and impact. Old manifest-less directories remain preserved and classified LEGACY_UNVERIFIED; do not infer their scenario or metrics.
5. Provide read-only inventory/diagnostic output for unverified legacy directories and exact rejected reasons. Do not rewrite legacy outputs into apparently valid manifests.

Verification: Tests for equivalent-input fingerprint, unique run IDs, atomic readable updates, legal/illegal status transitions, malformed/partial/failed/cancelled manifests, hash tampering, path traversal/absolute/symlink escape, and deterministic latest selection independent of impact/mtime.

Target File: D:\sih_work\floodsight\run_pipeline.py.

Exact Location: execute_full_simulation and its geometry, breach, SWE, snapshots, exposure/ranking, validation and result blocks; _solve_arm helper if used.

Objective: Wire canonical scenario, physical gates, unified graph and durable run state through the actual execution path.

Scope: Preserve useful raster/export/validation behavior; remove misleading fabricated fallbacks. Fail closed before success when required physics/geometry are missing. Every API and direct pipeline run uses the same path.

Contracts: Preserve existing arguments and add an optional resolved scenario manifest/analyst manifest path and cancellation callback only as needed. Custom files must exist. Resolve defaults from canonical scenario without silently overriding user WSE/fill/duration. Return manifest_path and validity alongside registered output paths. Important derived artifacts contain run/scenario/config/input identity, classification and uncertainty. Consequences are explicitly central_only until all arms truly propagate.

Micro-Steps for 5.6 Luna:
1. Create or attach to a queued run manifest; record failures and cancellations even for direct calls. Resolve scenario/inputs and hash them. Reject missing required canonical geometry/source identity before expensive fetching/solver work, with detailed preflight reasons. Do not mark all data observed based on old labels.
2. Validate DEM/cache coverage, metric units and actual canonical geometry after coarsening. Remove independent Annamayya carve/flowline and lowest-cell automatic snapping, arbitrary WSE reset, circular barrier and fallback volume. Coarsening must preserve geospatial cell-center placement and cannot create a passing unresolved breach.
3. Build graph hydrology through generic nodes for cascade and single-dam cases. Consume the full continuous trajectory, including pre-breach spillway transfer; shift solver time with a recorded event offset. Send total downstream transfer with separated source identity, not only breach Q. Bind spatial source to the graph terminal outlet's actual structure. Preserve explicit requested duration and record incomplete temporal coverage; remove the universal 20000-second floor.
4. Use graph interval flow semantics and validated opening weights in SWE. Do not initialize level-pool storage again in the 2D domain. Persist node/edge ledgers, actual solver/backend and numerical/geometry validation. Reject mass residual above 0.1 percent or correction above a separately documented budget; report measured terms.
5. Generate pre/post frames and event timestamps from that same state. Lake raster volume must match its stage-storage source within the declared tolerance. Remove arbitrary fraction frames and forced breach labels. Write lightweight index metadata with frame identity, simulation/event time, stage, preview bounds, sizes and provenance. Use native solver arrival grid at the same 0.30 m threshold, not saved-frame arrival reconstruction.
6. Keep envelope hydraulics only if every arm succeeds; propagate partial arm failure visibly. Mark exposure/isolation/ranking/validation central_only and remove statistical confidence claims. Remove active scenario-SPH comparator; retain actual idealized Ritter numerical benchmark with its own time/coordinates and classification.
7. Explicit LULC is authoritative when supplied; do not overwrite it with population roughness. Reproject rasters using transforms/CRS. Capture unavailable optional inventories, comparisons and derivatives in component states instead of turning failures into zero outcomes.
8. Register artifacts after successful writes and strict JSON validation. Attach provenance to GeoJSON collections/properties, hydrographs, rasters/tags, CAP and metric summaries. Complete the manifest only after all required gates and artifacts pass; exceptions keep partial files for diagnosis and set failed status.

Verification: End-to-end offline hypothetical valley fixture covers scenario→DEM→geometry→graph→SWE→rasters→exposure→manifest. Failure fixtures prove invalid geometry/forcing/artifact does not reach completed valid state. Attempt actual Annamayya and another Indian preflight/reconstruction, preserving logs and precise source gaps. Never claim those historical runs passed if the source geometry is missing or contradictory.

Target File: D:\sih_work\floodsight\src\api\worker.py (new) and D:\sih_work\floodsight\src\api\main.py.

Exact Location: RunRequest, run_simulation_endpoint, _async_job_runner, _rehydrate_saved_scenarios, _job_or_404, all result/export/frame/validation endpoints and scenario/context endpoints.

Objective: Bounded responsive execution, durable status, effective cancellation and valid-only file access.

Scope: Preserve route names where practical. Support the documented single FastAPI service; do not auto-launch unrelated processes. Use one bounded worker with bounded pending queue, launched on demand as a hidden Windows subprocess and cleaned up when done/cancelled.

Contracts: POST /api/run validates known scenario or an explicitly supplied analyst manifest, finite WSE, fill in (0,1], duration in (0,86400], integer coarsen in [1,32], supported failure/dam modes and positive optional engineering dimensions. Custom paths must resolve under data/analyst_inputs and exist; reject invalid requests with 422 before queuing. Queue saturation returns 429/503. POST /api/cancel/{job_id} cancels queued/active work and permanently gates its partial artifacts. GET /api/manifest/{job_id} exposes sanitized manifest state. Existing done status may adapt completed valid manifests for frontend compatibility, but failed/partial/invalid cannot serve outputs.

Micro-Steps for 5.6 Luna:
1. Replace in-process CPU BackgroundTasks with bounded subprocess supervision. Maintain queue/process handles under a lock; write request/manifest before launch. Terminate only the tracked child for cancellation, preserve partial artifacts, and ensure shutdown cleans owned workers. Use CREATE_NO_WINDOW or equivalent hidden process creation on Windows.
2. Worker invokes the shared pipeline and atomically records progress/status. API status reads durable state. On restart, recognize missing workers as interrupted; never infer completion. Remove both name/bounds-based archive recovery paths and import-time reading of all results.
3. Use one manifest resolver for every output route, including hydrographs, Ritter, SPH, lake, roads, envelopes and observations. Missing registered artifact is unavailable; no guessed sibling file. Restore identical validation/export availability from manifest after restart.
4. Scenario metadata remains a keyed dictionary from get_scenario_manifest. Structure layer uses its canonical geometry only. SAR routes call the scenario/date/AOI validator and never bypass it through old caches or create observations with fixed dates. Missing context returns empty FeatureCollection with NOT_AVAILABLE/reason metadata, distinct from observed empty inventory.
5. Snapshot metadata exposes frame_idx, t_s/t_min, event_time_iso, stage/phase_title, bounds, vector_url and optional raster_url, byte sizes, hashes and provenance; omit local paths. Selected endpoints check index bounds and registered artifact hash. Return ETag/content-size headers. Lightweight metadata must not load every frame.
6. Latest endpoint considers only valid manifests and stable completed-time ordering. CAP/SHP paths refer to actual JSON/ZIP artifacts. Benchmark endpoint labels static data legacy/unverified and never returns legacy simulated numbers as current output.
7. Where practical, roads/observations accept bounded WGS84 bbox and simplification tolerance, with source/provenance retained. Validate request bounds; do not hide layers to meet budgets.

Verification: Add API tests for validation, path escape, latest selection, archive restart, missing/modified artifacts, blocked partial output, bounded concurrent submissions, responsiveness during a worker run, and effective cancellation. Update old tests that directly inject unmanifested jobs so they either expect rejection or build a valid synthetic manifest. Do not add permissive bypasses for tests.

Target File: D:\sih_work\floodsight\src\m8_outputs\exporters.py.

Exact Location: _truncate_unique, export_shp, export_kml, export_cap_json.

Objective: Reusable complete geometry exports and unambiguous reconstruction alerts.

Scope: Preserve raw SHP creation, add ZIP download packaging. CAP is a JSON draft representation, not a claim of schema-certified SACHET compatibility.

Contracts: Export functions accept optional run/provenance context. CAP defaults Exercise and rejects Actual until a separate explicit operational authorization path exists. Output is strict JSON with null for missing numbers and actual executed solver identity. Multipart polygons/holes and empty results are supported.

Micro-Steps for 5.6 Luna:
1. Ensure unique <=10-character SHP names even when generated suffixes collide with existing names; package .shp/.shx/.dbf/.prj and field mapping in a ZIP, checking required sidecars.
2. Reproject KML/CAP to WGS84. Emit each MultiPolygon part and retain KML holes; handle empty geometry/results and missing numeric properties without exception or fabricated zero.
3. CAP uses unique run identity and provenance, neutral Exercise certainty/urgency when unsupported, and derives severity only from documented valid modeled metrics. Remove Actual/Immediate/Extreme/Likely defaults, fixed government-like sender and ANUGA/SPH claims. Preserve no-transmission behavior.

Verification: Round-trip SHP ZIP with GeoPandas; test colliding field names, projected MultiPolygon/holes, empty results, NaN normalization, Exercise default and rejected Actual mode.

## Consequence correctness

ARCHITECT BLUEPRINT FOR LUNA

Target File: D:\sih_work\floodsight\src\rasterutils.py.

Exact Location: sample_raster and demo; new shared geometry mask helper adjacent to sample_raster.

Objective: Preserve unknown coverage and use consistent settlement rasterization.

Contracts: sample_raster accepts optional nodata; returns NaN for off-grid/nonfinite/nodata and true zero for modeled dryness. The geometry helper returns valid in-geometry cells on the supplied transform/grid using one explicit all_touched policy shared by M5/M6. Empty inputs remain supported.

Micro-Steps for 5.6 Luna:
1. Validate 2D arrays, matching finite coordinates and invertible affine. Avoid rowcol for nonfinite coordinates.
2. Return missing coverage as NaN and update all M5/M6/M10-road callers so NaN comparisons cannot imply dry/safe.
3. Add windowed geometry-mask helper; never clamp outside geometry to a raster edge. Update demo assertions to the new honest contract.

Verification: Cell centers/edges, rotated affine, outside/nodata/NaN coordinates, empty input and subpixel geometry.

Target File: D:\sih_work\floodsight\src\m5_exposure\exposure.py.

Exact Location: compute_village_exposure and load_custom_population_csv.

Objective: Correct population alignment, polygon denominators and aggregate counts.

Scope: Retain proxy settlement footprints with explicit classification; do not pretend they are administrative census polygons.

Contracts: Missing population/inventory/coverage yields null plus status/reason. Explicit complete empty inventory may yield zero. Return per-settlement figures plus accessible aggregate metadata/helper for union/unique-asset totals; overlapping rows cannot be summed. Reject duplicate village IDs, malformed/nonfinite/negative headcounts and conflicting repeated CSV names.

Micro-Steps for 5.6 Luna:
1. Read masked depth. Count wet/valid cells inside the actual polygon, excluding the crop bounding box. Reuse the M6 geometry policy.
2. Reproject wet and valid-coverage masks onto population transform/CRS with rasterio.warp.reproject; reproject village polygons before population masking. Equal array shape does not establish alignment. Preserve partial/missing coverage.
3. Label any headcount apportioned by area PROXY_DATA, including a surveyed total. Missing OSM totals remain unavailable; explicit observed zero stays zero.
4. Sample building footprints for flooding rather than centroid only; count each uniquely identified asset once in union totals. Preserve proxy loss classification and currency assumptions, not surveyed monetary damages.
5. Record footprint overlap and compute run totals from union geometry/population cells and unique building/facility IDs. Pipeline must consume these aggregates instead of summing overlapping rows.

Verification: Shifted population raster formerly produced 10 exposed with no wet overlap; expected zero. Fully wet triangle formerly produced 60 percent and 60/100 exposed; expected 100 percent and 100. Test equal-sized different-CRS grids, partial/nodata/outside coverage, unknown versus zero, overlap union count and footprint flood missed by centroid.

Target File: D:\sih_work\floodsight\src\m6_isolation\isolation.py.

Exact Location: edge_midpoints, cut_flooded_edges, compute_isolation_times and emit_road_cut_timeline.

Objective: Whole-road flood sampling and defensible directed escape connectivity.

Contracts: Retain public signatures where possible; add explicit safe_destination_ids and max_access_distance_m. No automatic safe set of every dry node. If no defensible destinations exist, isolation_status is NO_DESTINATION_DATA and time remains unavailable. Other statuses distinguish ISOLATED, NOT_ISOLATED_IN_WINDOW, NO_ROAD_DATA and OUTSIDE_MODEL_DOMAIN. Bridge threshold override is optional and explicitly an assumption; no implicit 3 m deck clearance. Water arrival remains independently computable.

Micro-Steps for 5.6 Luna:
1. Precompute cells intersecting full edge geometry, including bent/multipart roads; reuse for graph cuts and exported timeline. An edge touching unavailable coverage cannot certify a safe route.
2. Validate all stack frames against the maximum raster grid and ordered timestamps. Reuse settlement mask; remove clipped-centroid fallback.
3. Find nearest nodes with metric distance and reject excessive access distance. Empty/unmapped graph remains missing data. Do not let the village's own dry node prove escape.
4. Use explicit covered/dry destinations and preserve directed graph reachability, efficiently through reverse multi-source traversal. Record unknown bridge/road coverage separately from modeled flood closure.
5. Keep threshold policy identical across isolation/timeline. Correct evacuation-window wording: positive arrival-minus-isolation is time between losing access and water arrival, not available evacuation access.

Verification: Endpoint-wet/midpoint-dry and bent roads; dry settlement isolated by its only flooded link; one-way routes; distant node; empty graph; missing destinations; outside/nodata road; identical cut times; inconsistent stack rejection. Preserve explicit hypothetical bridge-threshold test with honest classification.

Target File: D:\sih_work\floodsight\src\m7_ranking\ranker.py.

Exact Location: rank_villages and module documentation.

Objective: Central-only ranking with explicit missing inputs and no fabricated bands/egress.

Micro-Steps for 5.6 Luna:
1. Delete unused iso_for_score logic; preserve least urgency for modeled never-isolated. Unknown isolation remains unavailable, distinct from never isolated.
2. Remove fixed 15-percent bands; score_lo/score_hi remain null with consequence_scope central_only. Remove n_exits=1 default; keep missing egress unknown.
3. Validate one-to-one village merge and carry component availability, ranking coverage and scoring policy. Do not silently convert missing population/facilities/access to zero risk; incomplete ranking is explicitly provisional/unavailable as appropriate.
4. Correct the documentation's equal-weight and full-ensemble claims and preserve status/provenance columns in outputs.

Verification: Early isolation versus never-isolated order, unknown access, null uncertainty bands, no invented exits, duplicate IDs and missing component handling.

Target File: D:\sih_work\floodsight\src\m4_solvers\roughness.py; D:\sih_work\floodsight\src\m10_validation\roads.py; D:\sih_work\floodsight\src\data_fetcher.py.

Exact Location: load_manning_from_lulc_raster; _sample_along/compare_roads; build_villages/fetch_population.

Objective: Complete the transform/coverage contract across derived inputs and comparisons.

Contracts: LULC loader requires target_transform and target_crs alongside target_shape and uses categorical nearest-neighbor reprojection. Unknown codes/coverage must be reported or rejected. Optional population retrieval validates reference grid and tile/AOI coverage, preserving nodata.

Micro-Steps for 5.6 Luna:
1. Reproject LULC by actual grid; supplied invalid LULC raises, and pipeline cannot overwrite valid LULC with a population proxy.
2. M10 roads excludes unknown samples/links from observed-versus-modeled denominators; no NaN-as-dry comparisons.
3. Label cached/new settlement point buffers PROXY_DATA. For new footprints use explicit metric buffering; preserve raw OSM IDs/points. Missing OSM population is null, not zero.
4. Validate population cache against the actual reference grid; multi-tile AOIs require all intersecting tiles or explicit incomplete coverage. Do not zero-fill unavailable tiles or overwrite old caches silently.

Verification: Shifted/equal-shape/different-CRS LULC; no-overlap/nodata/classes; explicit LULC precedence; road unknown exclusion; population boundary/coverage and missing totals.

## Frontend truth and bounded loading

ARCHITECT BLUEPRINT FOR LUNA

Target File: D:\sih_work\floodsight\frontend\map.js.

Exact Location: SCENARIO_DAMS, _buildDamGeoJSON, _loadContextLayers, _getFallbackShelters, _updateShelterMarkers, updateScenarioDefaults, _loadSimulationRun, _loadSnapshotFrames, _applySnapshotFrame, _transitionToFloodFrame, _updateSurgeWaveMarker, _loadDemoResults, runSimulation and road layer filters.

Objective: Consume backend scenario/run truth and selected frames with bounded memory.

Scope: Preserve visual identity, panels, map layers and working controls. No aesthetic redesign. Start this phase only after API geometry/manifest/output gates are in place. Do not hide data layers as a performance strategy.

Contracts: GET /api/scenarios/metadata remains a keyed dictionary of canonical manifests. GET /api/manifest/{job_id} supplies event_clock, timeline_events, consequence_scope and sanitized artifact/validation metadata. GET /api/snapshots/{job_id} supplies metadata frames with frame_idx/time/stage/bounds/URLs/sizes/hash/provenance. Missing fields never cause invented geometry, timestamps, shelter names or numeric metrics. Expose a bounded performance snapshot through window.FloodSightPerformance with measured request/switch/frame timings and explicit unavailable browser measurements.

Micro-Steps for 5.6 Luna:
1. Remove hardcoded SCENARIO_DAMS data and replace references with a narrow adapter to canonical API fields. Generate dropdown/defaults/context/geometry from metadata. Candidate geometry remains visibly unverified and must not render as physical hydraulic truth. Remove frontend reservoir/outflow/crest/cascade-path invention.
2. Clear all run-owned state and currentJobId on scenario changes: frame caches, floods/roads/envelope, comparison data, timeline, export enablement and pending requests. Assign archive-loaded job ID before charts/downloads. Use AbortController plus generation token to reject stale responses across every scenario/run loader.
3. Remove fallback shelters and safe-haven declarations for ordinary facilities. Display facilities with actual source/type; a shelter requires verified designation/access metadata. No evidence means NOT_AVAILABLE. Remove interpolated Annamayya surge and farthest-point-as-wavefront animations unless a backend artifact supplies modeled positions.
4. Fetch snapshot metadata first and request only the selected representation. Keep all frame identities, including failures. Raster mode must not fetch/upload duplicate vectors. Use bounded LRU for raster blobs/object URLs and vector frames (for example four frames), revoke object URLs on eviction/reset, and optionally prefetch at most one adjacent frame only after selected display. Never all-frame Promise.all or all-PNG prefetch.
5. Frame selection is asynchronous and stale-safe. Apply the newest requested frame after its data arrives; report per-frame failure without compressing the index. Playback advances only when the next selected frame is ready. Preserve keyboard/range accessibility and avoid intercepting editing keys in input/select/textarea/contenteditable controls.
6. Read CAP preview from the server export; remove duplicate client alert generation. Road cut filters require finite numeric cut_time_min, not mere property existence. Rename misleading autoload/demo helper after checking references.
7. Measure first map render, first selected frame response, frame-switch durations, bytes, active layer count, bounded sample history, cache size and playback FPS. Heap uses performance.memory only when available; CPU remains NOT_AVAILABLE unless browser profiling provides it. Never substitute a fabricated metric.

Verification: Browser/fetch-spy tests prove metadata plus one selected payload, no preload, LRU capacity/URL revocation after 100 switches, stale A response cannot change B, archive job ID correctness, null road cuts open, inputs keep keyboard access, missing geometry/data remain unavailable. Record actual warm metadata/frame latency, p50/p95, bytes, layers, heap and FPS; do not claim thresholds from code inspection.

Target File: D:\sih_work\floodsight\frontend\spine.js.

Exact Location: fmtT, setEvents, setDomain and event rendering.

Objective: Scenario-timezone event clock and manifest-derived phase timeline.

Contracts: Event source is run manifest timeline_events plus explicitly modeled result arrivals; each event has sim_time_s, label, kind, classification/source and payload. A nullable origin means relative simulation time only.

Micro-Steps for 5.6 Luna:
1. Remove Annamayya and universal lake/spillway/breach event literals.
2. Map simulation seconds to event origin using Intl.DateTimeFormat with the manifest IANA timezone, not browser-local time.
3. Preserve origin uncertainty/classification and actual simulation offset. Empty event source yields an empty timeline state, not synthetic events. Keep stable frame identity and exact provided time domain.

Verification: Same event time under different browser-local timezones; negative/positive/unknown-origin times; no invented events for missing metadata; failed-frame identity preserved.

Target File: D:\sih_work\floodsight\frontend\charts.js and D:\sih_work\floodsight\frontend\index.html.

Exact Location: renderRitterPlot, renderScenarioSolverComparison, renderMalpassetBenchmark, renderDemoHydrograph, static Malpasset panel and scenario options.

Objective: Display only executed comparable solver results and source-backed comparisons.

Micro-Steps for 5.6 Luna:
1. Remove synthetic Delft3D analytical-plus/minus curves and unsupported solver claims. Plot FV/Ritter/SPH only with each series' real coordinates, time and benchmark classification. Scenario SPH unavailable must render its reason without charts/metrics.
2. Key/reset comparison cache by run ID. Remove current-looking static Malpasset simulated values; display legacy/unverified status until a current manifest registers actual comparison artifacts.
3. Remove unreachable demo hydrograph after checking references. Populate scenario selection from canonical metadata including unavailable Indian catalog cases and reasons.
4. Central-only consequence labeling replaces fake uncertainty; null numeric values render unavailable rather than zero. Do not label ordinary model estimates historical observations.

Verification: JavaScript syntax checks; no Delft3D fabricated arrays/static current-model values; run switches reset charts; incomplete/unknown data renders correctly.

Target File: D:\sih_work\floodsight\frontend\ui.js and D:\sih_work\floodsight\frontend\debug.js.

Exact Location: EVENT_DESC, showEventPop and debug logging helpers/callers.

Objective: Remove duplicate event/shelter authority and bound instrumentation/log history.

Micro-Steps for 5.6 Luna:
1. Read descriptions/event popup source fields from canonical metadata; remove fixed per-scenario shelter/capacity assertions.
2. Route direct debug log.push callers through the bounded helper and keep performance samples bounded. Preserve useful console/error diagnostics and hidden-tab repaint behavior.
3. Preserve existing responsive/accessibility behavior and update root ui.md only with verified reusable implementation facts, not generic design advice.

Verification: Syntax checks plus desktop/mobile rendered-state verification, repeated polling/log-cap tests, hidden-tab resume and missing-data popup checks.

## Completion and evidence

After implementation, run both pytest -q and python -m pytest -q once on the final source state; address genuine regressions and obsolete assertions. Run focused physics/integrity/geospatial tests plus actual Annamayya and second Indian preflight/reconstruction attempts. Preserve results and failures honestly. Inspect generated geometry/timelines/provenance and the actual browser; performance targets remain targets until measured.

Luna writes FLOODSIGHT_IMPLEMENTATION_REPORT.md with all thirty FS findings classified FIXED, PARTIALLY_FIXED, NOT_FIXED or NOT_APPLICABLE, corrected review interpretations, changed symbols, before/after tests, new independent findings, architecture/physics/data/API/UI details, acceptance-gate status, blockers and exact next steps. Update the existing implementation plan statuses with evidence. Create root rearch_finding.md only for substantive dated source-backed findings, best_way.md for verified workflow knowledge, and ui.md for verified UI patterns; preserve existing unrelated notes and append-only research history. Update misleading active README/module claims, quarantine unsupported wrappers/diagnostics only after checking references, and preserve all historical artifact files. No mass deletion or unsolicited deployment.
