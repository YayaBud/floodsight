/**
 * FloodSight — Map.js
 * MapLibre GL JS terrain map, flood depth layer, road status overlay,
 * rivers layer, buildings layer, arrival-time isochrones,
 * time-scrubber integration, and village interaction.
 *
 * Phase 0: All fabricated data removed. No counter, badge, or callout text
 * may display a value that was not computed by the pipeline.
 */

"use strict";

// ── State ─────────────────────────────────────────────────────────────────
let map = null;
let currentJobId = null;
let simulationResults = null;
let snapshotFrames = [];         // [{t_s, t_min, geojson}] loaded after run
let currentStep = 0;
let frameTransitionId = 0;
// The id of the village whose isolation callout is currently displayed, or
// null when none is. Was a boolean, which could only ever announce one village
// per playback.
let isolationShown = null;
let playTimer = null;
// Same defect as currentBasemap below: assigned in runSimulation but never
// declared, so under "use strict" the assignment threw ReferenceError inside
// the road-loading try/catch. The catch logged "Road timeline not available"
// and the road layer stayed empty on every run.
let roadsGeoJSON = null;
// Assigned by switchBasemap(). Was never declared: under "use strict" the
// assignment threw ReferenceError, so every basemap button was dead.
let currentBasemap = "streets";

// ── Tactical Map Markings State ───────────────────────────────────────────
let damMarker = null;
let damPopup = null;
let preDamMarker = null;
let preDamPopup = null;
let subDamMarker = null;
let subDamPopup = null;
let cascadeMarker = null;
let cascadePopup = null;
let villageMarkers = [];
let wavefrontMarker = null;
let roadCutMarkers = [];
let observedMarker = null;
let currentObservedGeoJSON = null;
let mapMarkingsVisible = true;
let shelterMarkers = [];
let sheltersVisible = true;

// ── New run-layer state (lake, water planes, front field, evac routes) ─────
let lakeFrames = [];          // [{t_min, level_m, volume_mcm, area_km2, phase, source, geojson}]
let lakePopup = null;
let waterPlanesPopup = null;
let frontField = null;        // {west,south,east,north,nx,ny,t_arr_min,u,v,provenance}
let flowFieldOn = true;
let evacRoutesGeoJSON = null;
let evacRoutePopup = null;
let selectedEvacVillageId = null;
let storyRunning = false;

const SCENARIO_PRESENTATION = {
  rishiganga: {
    name: "Rishi Ganga Avalanche Barrier & Cascade",
    sub: "2,450 m WSE · 70 m Barrier Height",
    _fallbackLon: 79.71228,
    _fallbackLat: 30.46915,
    wse: 2450,
    height: 70,
    crest_length_m: 350,
    vol: 15.0,
    bed_elevation_m: 2380,
    type: "Landslide / Rock-Ice Avalanche Dam",
    event: "7 Feb 2021 Chamoli Outburst Disaster",
    breach_mode: "High-Altitude Rock-Ice Avalanche Damming & Outburst",
    breach_short: "AVALANCHE OUTBURST",
    q_peak: "~25,000 m³/s",
    desc: "A massive rock and ice avalanche from Ronti Peak impounded the Raunthi Gad confluence gorge, creating an ephemeral barrier that failed catastrophically.",
    // event_clock removed (P1-2/FS-19/§I): the server is the one authority
    // for event timing now (see _getDamData in this file, and
    // src/scenarios.py's load_event_clock). No timestamp lives in JS.
    cascade: {
      type: "Avalanche Damming & Downstream HEP Cascade",
      upstream_name: "Ronti Avalanche Scar (5,500m)",
      upstream_status: "Detached (T-10 min)",
      subdam_name: "Rishiganga Small Hydro Project (HEP)",
      subdam_status: "Obliterated at T+10 min",
      downstream_name: "Tapovan Vishnugad Barrage (NTPC)",
      downstream_status: "Barrage destroyed at T+35 min",
      distance_km: 11.2,
      transit_min: 35,
      chain_desc: "Ronti Peak avalanche triggers debris dam -> bursts -> obliterates Rishiganga HEP -> tears downstream to demolish Tapovan Vishnugad barrage",
    },
    focus_pitch: 52,
    focus_bearing: 305,
    focus_zoom: 14.5,
  },
  phutkal: {
    name: "Phutkal Landslide Dam",
    sub: "3,878 m WSE · 58 m Dam Height",
    _fallbackLon: 77.05783,
    _fallbackLat: 33.25242,
    wse: 3878,
    height: 58,
    crest_length_m: 260,
    vol: 30.0,
    bed_elevation_m: 3820,
    type: "Valley Landslide Impoundment",
    event: "May 2015 Zanskar Blockage & Outburst",
    breach_mode: "Landslide Dam Progressive Overtopping & Canyon Trenching",
    breach_short: "LANDSLIDE OVERTOPPING",
    q_peak: "~2,800 m³/s",
    desc: "Catastrophic cliff collapse blocked the deep Tsrap Chu gorge for months before sudden overtopping through an artificial channel washed out bridges across Zanskar.",
    cascade: {
      type: "Canyon Lake Outburst & Downstream Destruction",
      downstream_name: "Phuktal Monastery & Tsarap Bridges",
      downstream_status: "Suspension Bridges Swept Away",
      distance_km: 12.0,
      transit_min: 30,
      chain_desc: "Limestone cliff collapse impounds 15 km Tsarap Chu lake -> artificial diversion overtopping trenches 58m dam -> sweeps downstream bridges and Dorzong",
    },
    focus_pitch: 50,
    focus_bearing: 320,
    focus_zoom: 14.5,
  },
  derna: {
    name: "Abu Mansour Dam (Upper Derna)",
    sub: "170 m WSE · 74 m Dam Height",
    _fallbackLon: 22.57733,
    _fallbackLat: 32.65755,
    wse: 170,
    height: 74,
    crest_length_m: 300,
    vol: 22.5,
    bed_elevation_m: 96.0,
    type: "Clay-Core Rockfill Embankment",
    event: "11 Sep 2023 Storm Daniel Disaster",
    breach_mode: "Storm Daniel Severe Overtopping & Progressive Embankment Washout",
    breach_short: "OVERTOPPING & CASCADE BREACH",
    q_peak: "~9,500 m³/s",
    desc: "Extreme rainfall from Storm Daniel filled the reservoir beyond crest, causing total collapse and sending a 22.5 MCM torrent that wiped out Al-Bilad downstream.",
    cascade: {
      type: "Dual Embankment Cascade",
      upstream_name: "Abu Mansour Dam (Upstream)",
      upstream_status: "Breached (02:30 AM)",
      downstream_name: "Al-Bilad Dam (Downstream City Dam)",
      downstream_status: "Destroyed at T+30 min (03:00 AM)",
      distance_km: 13.7,
      transit_min: 30,
      chain_desc: "Storm Daniel overwhelms Abu Mansour -> 22.5 MCM surge races 13.7 km down Wadi Derna -> instantly annihilates 45m Al-Bilad Dam -> sweeps through Derna city into Mediterranean",
    },
    focus_pitch: 48,
    focus_bearing: 40,
    focus_zoom: 14.5,
  },
  south_lhonak: {
    name: "South Lhonak Moraine Dam & Glacial Lake",
    sub: "5,200 m WSE · 60 m Moraine Height",
    _fallbackLon: 88.18742,
    _fallbackLat: 27.91478,
    wse: 5200,
    height: 60,
    crest_length_m: 310,
    vol: 50.0,
    bed_elevation_m: 5140,
    type: "Glacial Moraine Dam + Hydro Gravity Dam",
    event: "4 Oct 2023 Teesta GLOF",
    breach_mode: "Moraine Dam Collapse Triggered by Ice Avalanche / GLOF",
    breach_short: "GLOF MORAINE FAILURE",
    q_peak: "~55,000 m³/s",
    desc: "Glacial lake outburst flood produced massive surge waves that tore down Teesta canyon through Chungthang concrete-rockfill dam and swept the Teesta basin.",
    cascade: {
      type: "GLOF Moraine Outburst & Hydro Cascade",
      upstream_name: "South Lhonak Glacial Lake",
      upstream_status: "Moraine Breach (T+0 min)",
      downstream_name: "Chungthang Dam (Teesta III HEP)",
      downstream_status: "Washed away in 10 min (T+75 min)",
      distance_km: 42.0,
      transit_min: 75,
      chain_desc: "Ice-rock avalanche triggers South Lhonak moraine breach -> 7,500 m³/s surge rushes down Teesta basin -> completely washes away 60m Chungthang concrete-rockfill dam",
    },
    focus_pitch: 54,
    focus_bearing: 120,
    focus_zoom: 14.2,
  },
  ivanovo: {
    name: "Ivanovo Dam (Bulgaria)",
    sub: "171 m WSE · 16 m Embankment Height",
    _fallbackLon: 25.85388,
    _fallbackLat: 41.86243,
    wse: 171,
    height: 16,
    crest_length_m: 180,
    vol: 2.5,
    bed_elevation_m: 155.0,
    type: "Earthen Embankment Dam",
    event: "6 Feb 2012 Biser Flood",
    breach_mode: "Rapid Snowmelt & Heavy Rainfall Crest Overtopping Collapse",
    breach_short: "EMBANKMENT OVERTOPPING",
    q_peak: "~800 m³/s",
    desc: "Intense thaw and downpours overtopped the earthen dam wall, blasting open a 20-meter breach that submerged Biser village within minutes.",
    cascade: {
      type: "Valley Embankment Burst & Village Submergence",
      downstream_name: "Biser Village Flood Dykes & Bridge",
      downstream_status: "Submerged by 3-4m Flood Wave (T+20 min)",
      distance_km: 3.8,
      transit_min: 20,
      chain_desc: "Rapid snowmelt and downpours overtop earthen crest -> 20m embankment breach blasts open -> flood surge inundates Biser village in minutes",
    },
    focus_pitch: 45,
    focus_bearing: 65,
    focus_zoom: 14.8,
  },
  malpasset: {
    name: "Malpasset Arch Dam (France)",
    sub: "101.5 m WSE · 66.5 m Arch Height",
    _fallbackLon: 6.75684,
    _fallbackLat: 43.51216,
    wse: 101.5,
    height: 66.5,
    crest_length_m: 222,
    vol: 50.0,
    bed_elevation_m: 35.0,
    type: "Thin Concrete Arch Dam",
    event: "2 Dec 1959 Reyran Valley Disaster",
    breach_mode: "Left Abutment Foundation Slip & Instantaneous Arch Collapse",
    breach_short: "INSTANTANEOUS ARCH BREAK",
    q_peak: "~8,000 m³/s",
    desc: "Interstitial pore-water pressure along an unnoticed tectonic clay seam sheared the left rock foundation, causing the entire thin-arch concrete wall to blow out instantaneously.",
    cascade: {
      type: "Canyon Arch Collapse & Coastal Inundation",
      downstream_name: "Bozon Highway Bridge & Frejus Estuary",
      downstream_status: "Bridge Destroyed & City Inundated (T+20 min)",
      distance_km: 8.0,
      transit_min: 20,
      chain_desc: "Left rock abutment slip along tectonic foliation causes instantaneous arch collapse -> 40m surge wave sweeps down Reyran gorge to Fréjus in 20 min",
    },
    focus_pitch: 52,
    focus_bearing: 170,
    focus_zoom: 14.6,
  },
  annamayya: {
    name: "Annamayya Dam (Cheyyeru River)",
    sub: "215 m WSE · 25 m Dam Height",
    _fallbackLon: 79.02128,
    _fallbackLat: 14.21059,
    wse: 206.0,
    height: 26.0,
    crest_length_m: 630,
    vol: 63.43,
    bed_elevation_m: 180.0,
    type: "Earthen Embankment with Masonry Spillway",
    event: "19 Nov 2021 Cheyyeru River Outburst",
    breach_mode: "Spillway Under-Capacity, Gate Jam & Severe Embankment Overtopping",
    breach_short: "EMBANKMENT OVERTOPPING",
    q_peak: "~13,500 m³/s",
    desc: "Inflow from upstream Pincha ring bund washout (03:30 AM, EVD-04) and Cheyyeru catchment runoff routed to Annamayya. With gate #4 jammed (EVD-14), reservoir overtopped the +206.0m crest around 05:30-06:00 AM (EVD-16), and pre-washout overtopping discharge was already reaching the gorge by ~06:15-06:25 AM (EVD-21/22) before the dam's full 336m earthen section washout completed at 06:30 AM (T=0, EVD-17/18), sending the main breach wave down Cheyyeru to Penagaluru (EVD-28). Exact event timing: see the server-authored event clock.",
    cascade: {
      type: "Upstream Pincha Inflow Cascade",
      upstream_name: "Pincha Ring Bund (Upstream)",
      upstream_status: "Ring Bund Washed Out (03:15 AM / T-150 min, EVD-04)",
      downstream_name: "Annamayya Dam (Downstream)",
      downstream_status: "Overtopping at 05:45 AM (T=0, EVD-16); Bund Washout at 06:15 AM (EVD-17)",
      distance_km: 34.0,
      transit_min: 150,
      chain_desc: "Upstream Pincha ring bund washed out at 03:15 AM (EVD-04) -> 34 km surge down Cheyyeru overwhelmed jammed spillway gate #4 (EVD-14) -> Overtopped +206.0m crest at 05:45 AM (EVD-16) -> Complete bund collapse at 06:15 AM (EVD-17)",
    },
    focus_pitch: 45,
    focus_bearing: 35,
    focus_zoom: 13.5,
  },
};

// Real per-scenario hydraulic geometry, fetched from the backend (P1-1 /
// FS-12,13,14,22). NOT the same object as SCENARIO_PRESENTATION above --
// this one carries only what the geometry validator actually vouches for.
// A scenario with hydraulic_ready === false gets an empty geometry object
// here: drawing a fallback shape from anywhere else would be exactly the
// fabrication this fix exists to remove.
let SCENARIO_GEOMETRY = {};

async function _loadScenarioGeometry() {
  try {
    const meta = await fetch("/api/scenarios/metadata").then(r => r.ok ? r.json() : {});
    SCENARIO_GEOMETRY = meta || {};
  } catch (e) {
    console.warn("Could not load scenario geometry metadata:", e);
    SCENARIO_GEOMETRY = {};
  }
}

function _roleGeometry(scenarioKey, role) {
  const feats = SCENARIO_GEOMETRY[scenarioKey]?.geometry?.features || [];
  return feats.find(f => f.properties?.role === role)?.geometry || null;
}

function _lineStringToCoords(geom) {
  return geom && geom.type === "LineString" ? geom.coordinates : null;
}

function _polygonToRing(geom) {
  return geom && geom.type === "Polygon" ? geom.coordinates[0] : null;
}

function _pointToCoords(geom) {
  return geom && geom.type === "Point" ? geom.coordinates : null;
}

// Adapter returning an object shaped like the old pre-rename dam-data
// entries, so every existing call site below needs only its lookup swapped -- not its
// internal logic. Hydraulic fields are populated ONLY when the backend
// validator actually vouches for this scenario's geometry; otherwise they
// are omitted (undefined), and _buildDamGeoJSON's existing
// `if (dam.X && dam.X.length >= N)` guards already skip an undefined field
// cleanly -- no fabricated fallback shape is substituted.
function _getDamData(scenarioKey) {
  const presentation = SCENARIO_PRESENTATION[scenarioKey];
  if (!presentation) return null;
  const meta = SCENARIO_GEOMETRY[scenarioKey];
  const ready = !!meta?.availability?.hydraulic_ready;
  const breachPointGeom = ready ? _pointToCoords(_roleGeometry(scenarioKey, "breach_point")) : null;
  // lon/lat always come from the scenario's own resolved breach point in
  // data_fetcher.SCENARIOS (via the API), whether or not the surrounding
  // geometry validated -- a marker location is not the disputed hydraulic
  // claim; the axis/reservoir/river shapes are.
  const fallbackPoint = _pointToCoords(_roleGeometry(scenarioKey, "breach_point"));
  const [lon, lat] = breachPointGeom || fallbackPoint || [presentation._fallbackLon, presentation._fallbackLat];
  const dam = {
    ...presentation,
    lon, lat,
    hydraulic_ready: ready,
    geometry_reason: meta?.availability?.reasons?.[0] || null,
    // One server-authored event clock (FS-19/§I) -- overrides any stale
    // client-side origin_iso still on SCENARIO_PRESENTATION. A scenario with
    // no evidence file gets classification NOT_AVAILABLE / origin_iso null
    // from the backend, not a hardcoded fallback.
    event_clock: meta?.event_clock || { origin_iso: null, classification: "NOT_AVAILABLE", timeline_events: [] },
  };
  if (ready) {
    dam.dam_axis = _lineStringToCoords(_roleGeometry(scenarioKey, "dam_axis"));
    const barrier = _roleGeometry(scenarioKey, "dam_body") || _roleGeometry(scenarioKey, "blockage");
    dam.dam_body_polygon = _polygonToRing(barrier);
    dam.outflow_vector = _lineStringToCoords(_roleGeometry(scenarioKey, "river"));
  }
  // reservoir_pool is intentionally never populated -- no scenario has a
  // validated reservoir role yet (P1-1 scope). Leaving it undefined makes
  // _buildDamGeoJSON's existing guard skip the reservoir layer entirely
  // rather than draw something unbacked.
  return dam;
}

// Demo scenario data (Phutkal AOI, J&K — precomputed for demo)
// Empty until a run produces real output.
//
// This used to hold five invented villages — names, populations, isolation
// times and rupee losses — served on page load and labelled "PRECOMPUTED".
// They were illustrative figures from the design document, not the output of
// any computation, and a judge clicking the map before pressing Run would have
// been shown fabricated numbers wearing a provenance badge. The panels have an
// empty state; it is used instead.
const EMPTY_RESULTS = { type: "FeatureCollection", features: [] };

async function fetchJson(url, label) {
  const response = await fetch(url);
  const text = await response.text();
  let payload;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch (err) {
    throw new Error(`${label} returned invalid JSON (${response.status})`);
  }
  if (!response.ok) {
    const detail = payload && (payload.detail || payload.message);
    throw new Error(`${label} failed (${response.status})${detail ? `: ${detail}` : ""}`);
  }
  return payload;
}

// ── Basemap sources catalogue ───────────────────────────────────────────
// Note: Esri World Imagery requires a free ArcGIS developer account for
// non-revenue use. For a government submission, verify licence terms first.
// OSM and ESRI topo are key-less.
const BASEMAP_URLS = {
  streets: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
  satellite: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],
  topo: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}"],
  dark: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],  // desaturated below
};

// ── Initialise map ─────────────────────────────────────────────────────
function initMap() {
  const MAP_STYLE = {
    version: 8,
    // A raster-only style has no font source, and MapLibre refuses ANY layer
    // with a `text-field` without one: "layers.wse-anchor-label.layout
    // .text-field: use of \"text-field\" requires a style \"glyphs\" property".
    // That error fired every time the Reported-extent layer was switched on,
    // and the anchor labels ("Nandalur - 2.0-4.0 m") silently never drew.
    //
    // Noto Sans Regular is what this endpoint actually serves -- checked, the
    // MapLibre default stack (Open Sans Regular) 404s here, so `text-font` is
    // declared explicitly below rather than left to the default.
    //
    // If the endpoint is unreachable the labels are missing and nothing else
    // is: the halo, point and fill are not text layers, and the hover popup
    // carries strictly more than the label does.
    glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
    sources: {
      "osm": {
        type: "raster",
        tiles: BASEMAP_URLS.streets,
        tileSize: 256,
        maxzoom: 19,
        attribution: "© OpenStreetMap contributors",
      },
    },
    layers: [
      {
        id: "osm-tiles",
        type: "raster",
        source: "osm",
        paint: {
          "raster-opacity":        0.9,
          "raster-saturation":    -0.25,
          "raster-brightness-min": 0.0,
          "raster-brightness-max": 1.0,
          "raster-contrast":       0.0,
        },
      },
    ],
  };

  map = new maplibregl.Map({
    container: "map",
    style: MAP_STYLE,
    center: [79.73, 30.48],   // Rishiganga, Uttarakhand
    zoom: 9,
    // 2.5D, oblique and slightly rotated. The tilt reads the valley the water is
    // running down, which a plan view flattens away. Flattened to 0 once and
    // reduced to 30 once; both were rejected on sight, so these are the numbers.
    // Do not "fix" them.
    pitch: 40,
    bearing: -10,
    antialias: true,
    maxTileCacheSize: 150,
  });

  map.addControl(new maplibregl.NavigationControl(), "top-left");
  map.addControl(new maplibregl.ScaleControl(), "bottom-left");

  map.on("load", () => {
    // Each step is isolated. Previously a single throwing addLayer aborted the
    // whole handler, so every layer registered after the failure silently did
    // not exist -- the map looked half-built with no error pointing at why.
    const steps = [
      ["terrain",         _addTerrain],
      ["india boundary", _addIndiaBoundaryLayer],
      ["rivers",         _addRiversLayer],
      ["flood depth",    _addFloodLayer],
      // Water planes above the flood (Somasila must not read as flooded) but
      // BELOW the lake: the Annamayya reservoir's own DSM plane is a pale
      // wedge inside the lake outline and showed through it as a triangle.
      ["water planes",    _addWaterPlanesLayer],
      ["lake",            _addLakeLayer],
      ["envelope",       _addEnvelopeLayer],
      ["roads",          _addRoadLayer],
      ["evac routes",     _addEvacRoutesLayer],
      ["buildings",      _addBuildingsLayer],
      ["validation",     _addValidationLayers],
      ["dam structure",  _addDamStructureLayers],
      ["shelters",       _addSheltersLayer],
      ["villages",       _addVillageLayer],
      ["demo results",   _loadDemoResults],
      ["flow field",      _initFlowFieldCanvas],
      ["context layers", () =>
        _loadContextLayers(document.getElementById("scenario-select").value)],
    ];
    for (const [name, fn] of steps) {
      try {
        fn();
      } catch (err) {
        console.error(`[FloodSight] layer step "${name}" failed:`, err);
        if (window.FloodSightDebug) {
          console.error("[FloodSight] map state at failure:",
                        window.FloodSightDebug.state());
        }
      }
    }
    _updateDamSetupCard(document.getElementById("scenario-select")?.value || "rishiganga");
  });

  map.on("zoomend", () => {
    const isZoomedIn = map.getZoom() >= 10.2;
    document.querySelectorAll(".village-mark.is-dot-only").forEach(el => {
      el.classList.toggle("force-show-badge", isZoomedIn);
    });
  });

  // Viewport-dependent overlays: the road-cut marker set is filtered to what
  // is on screen, and marker labels are re-declashed after every pan/zoom.
  // Throttled to one pass per event rather than per pixel of drag.
  let _viewportRafId = null;
  const _onViewportChange = () => {
    if (_viewportRafId) return;
    _viewportRafId = requestAnimationFrame(() => {
      _viewportRafId = null;
      _lastRoadCutT = -99999; // force a re-filter even if tMin is unchanged
      _updateRoadCutMarkers(snapshotFrames.length ? snapshotFrames[currentStep].t_min : 0);
      _declutterVillageMarkers();
    });
  };
  map.on("moveend", _onViewportChange);
  map.on("zoomend", _onViewportChange);

  // Keyboard scrubber controls (§18: operators use keyboards under stress)
  document.addEventListener("keydown", (e) => {
    if (!snapshotFrames.length) return;
    const slider = document.getElementById("time-slider");
    if (e.code === "ArrowRight") {
      const next = Math.min(+slider.value + 1, snapshotFrames.length - 1);
      slider.value = next;
      onTimeSlider(next);
    } else if (e.code === "ArrowLeft") {
      const prev = Math.max(+slider.value - 1, 0);
      slider.value = prev;
      onTimeSlider(prev);
    } else if (e.code === "Space") {
      e.preventDefault();
      togglePlay();
    }
  });
}

// -- Terrain + hillshade -------------------------------------------------
// Runs first in `steps`, with no beforeId, so every subsequent addLayer()
// stacks on top by execution order alone — the hillshade ends up directly
// above the basemap raster and below every data layer.
function _addTerrain() {
  map.addSource("terrain-dem", {
    type: "raster-dem",
    tiles: ["https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"],
    tileSize: 256,
    maxzoom: 12,
    encoding: "terrarium",
  });
  map.setTerrain({ source: "terrain-dem", exaggeration: 1.2 });
  map.addLayer({
    id: "hillshade",
    type: "hillshade",
    source: "terrain-dem",
    paint: {
      "hillshade-exaggeration": 0.5,
      "hillshade-shadow-color": "#1a2233",
      "hillshade-highlight-color": "#ffffff",
      "hillshade-accent-color": "#3a4a63",
    },
  });
}

// -- India external boundary (Survey of India) -------------------------------
function _addIndiaBoundaryLayer() {
  map.addSource("india-boundary", {
    type: "geojson",
    data: "assets/india_boundary_jk.geojson",
  });

  map.addLayer({
    id: "india-boundary-line",
    type: "line",
    source: "india-boundary",
    paint: {
      "line-color": cssVar("--text-primary", "#151a19"),
      "line-width": 2.2,
      "line-opacity": 0.9,
    },
  });

  // NOTE: no third argument here. map.on(type, listener) takes TWO arguments.
  // A stray `, 3000` (setInterval arity, pasted in by mistake) makes MapLibre's
  // on(e,t,i) see a defined `i` and take the LAYER-SCOPED path instead: it then
  // treats this arrow function as the `layers` argument and calls
  // layers.filter(...) on it. That threw "t.filter is not a function" on every
  // moveend -- and resize() and every zoom fire moveend -- which escaped into
  // _render() and wedged MapLibre's task queue ("Attempting to run(), but is
  // already running"). That was the map freezing on zoom and on window resize.
  let _boundaryTimer = null;
  map.on("moveend", () => {
    if (_boundaryTimer) clearTimeout(_boundaryTimer);
    _boundaryTimer = setTimeout(() => {
      const b = map.getBounds();
      const inCrop = b.getWest() >= 71.5 && b.getEast() <= 82.5 &&
                     b.getSouth() >= 29.5 && b.getNorth() <= 38.0;
      const src = map.getSource("india-boundary");
      if (!src) return;
      const wantFull = !inCrop;
      if (src._floodsightIsFull !== wantFull) {
        src.setData(wantFull ? "assets/india_boundary_full.geojson"
                             : "assets/india_boundary_jk.geojson");
        src._floodsightIsFull = wantFull;
      }
    }, 200);
  });
}

// ── UI Helpers ────────────────────────────────────────────────────────────


// ── Village layer ─────────────────────────────────────────────────────────
function _addVillageLayer() {
  map.addSource("villages", { type: "geojson", data: { type: "FeatureCollection", features: [] } });

  map.addLayer({
    id: "villages-fill",
    type: "fill",
    source: "villages",
    paint: {
      "fill-color": [
        "interpolate", ["linear"],
        ["get", "priority_score"],
        0.0, priorityRamp()[0],
        0.5, priorityRamp()[1],
        1.0, priorityRamp()[2],
      ],
      "fill-opacity": 0.16,
    },
  });

  map.addLayer({
    id: "villages-outline",
    type: "line",
    source: "villages",
    paint: {
      "line-color": [
        "interpolate", ["linear"],
        ["get", "priority_score"],
        0.0, priorityRamp()[0],
        0.5, priorityRamp()[1],
        1.0, priorityRamp()[2],
      ],
      "line-width": 1.5,
      "line-opacity": 0.8,
    },
  });

  // Hover popup
  const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, maxWidth: "260px" });

  map.on("mouseenter", "villages-fill", (e) => {
    map.getCanvas().style.cursor = "pointer";
    const p = e.features[0].properties;
    const fmt = (v, unit) =>
      isMissing(v)
        ? '<span class="pop-na">not computed</span>'
        : `${(+v).toFixed(0)}${unit}`;
    // Same treatment as the priority list: OSM names are escaped before they
    // reach setHTML, and a missing score renders as a dash rather than "NaN".
    popup.setLngLat(e.lngLat).setHTML(`
      <div class="village-popup">
        <div class="pop-name" dir="auto">${escapeHtml(p.village_name)}</div>
        <div class="pop-rank">Rank ${escapeHtml(p.priority_rank)} &middot; score ${fmtFixed(p.priority_score)}</div>
        <dl class="pop-grid">
          <dt>Population at risk</dt><dd>${fmt(p.pop_at_risk, "")}</dd>
          <dt>Last road out</dt><dd>${fmt(p.isolation_time_min, " min")}</dd>
          <dt>Water arrives</dt><dd>${fmt(p.water_arrival_min, " min")}</dd>
          <dt class="pop-key">Evacuation window</dt>
          <dd class="pop-key">${fmt(p.evacuation_window_min, " min")}</dd>
        </dl>
        <div class="pop-prov">${escapeHtml(p.label || "NOT AVAILABLE")}</div>
      </div>
    `).addTo(map);
  });

  map.on("mouseleave", "villages-fill", () => {
    map.getCanvas().style.cursor = "";
    popup.remove();
  });

  map.on("click", "villages-fill", (e) => {
    const p = e.features[0].properties;
    showIsolationCallout(p);
  });
}

// ── Shelters & High-Ground Evacuation Havens Layer ────────────────────────
function _addSheltersLayer() {
  map.addSource("shelters", {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] }
  });

  map.addLayer({
    id: "shelters-halo",
    type: "circle",
    source: "shelters",
    paint: {
      "circle-radius": 14,
      "circle-color": "#10b981",
      "circle-opacity": 0.22,
      "circle-stroke-color": "#10b981",
      "circle-stroke-width": 1.5,
    }
  });

  map.addLayer({
    id: "shelters-point",
    type: "circle",
    source: "shelters",
    paint: {
      "circle-radius": 6,
      "circle-color": "#10b981",
      "circle-stroke-color": "#ffffff",
      "circle-stroke-width": 2,
    }
  });

  const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, maxWidth: "300px" });

  map.on("mouseenter", "shelters-point", (e) => {
    map.getCanvas().style.cursor = "pointer";
    const p = e.features[0].properties || {};
    const name = p.name || "Designated Relief Shelter";
    const amenity = (p.amenity || "shelter").toUpperCase();
    const typeLabel = p.type_label || (amenity === "HOSPITAL" ? "Emergency Medical Haven" : "High-Ground Relief Camp");
    const elev = p.elevation ? `Elev. ~${p.elevation}` : "Safe Elevation";
    popup.setLngLat(e.lngLat).setHTML(`
      <div class="village-popup" style="border-left: 3px solid #10b981;">
        <div class="pop-name" style="color: #10b981;">🛡️ ${escapeHtml(name)}</div>
        <div class="pop-rank" style="color: #34d399;">SAFE HAVEN · ${escapeHtml(typeLabel)}</div>
        <dl class="pop-grid" style="margin-top: 6px;">
          <dt>Status</dt><dd style="color:#10b981;font-weight:600;">ACTIVE SAFE HAVEN</dd>
          <dt>Elevation</dt><dd>${escapeHtml(elev)}</dd>
          <dt>Capacity</dt><dd>${escapeHtml(p.capacity || "500-1,500 persons")}</dd>
          <dt>Flood Risk</dt><dd style="color:#34d399;">Outside Inundation Zone</dd>
        </dl>
      </div>
    `).addTo(map);
  });

  map.on("mouseleave", "shelters-point", () => {
    map.getCanvas().style.cursor = "";
    popup.remove();
  });
}

function _updateShelterMarkers(geojson, scenarioKey) {
  if (!map) return;
  for (const m of shelterMarkers) m.remove();
  shelterMarkers = [];

  const features = (geojson && geojson.features) || [];
  if (!features.length) return;

  const displayed = features.slice(0, 5);
  for (const f of displayed) {
    const p = f.properties || {};
    const name = p.name || (p.amenity === "hospital" ? "Medical Relief Camp" : "High-Ground Shelter");
    const geom = f.geometry;
    let coords = null;
    if (geom.type === "Point") {
      coords = geom.coordinates;
    } else if (geom.type === "Polygon" && geom.coordinates && geom.coordinates[0]) {
      coords = geom.coordinates[0][0];
    }
    if (!coords) continue;

    const el = document.createElement("div");
    el.className = "map-mark shelter-mark";
    el.title = `EVACUATE TO: ${name} — Click to focus safe haven`;
    el.innerHTML = `
      <div class="shelter-pin">
        <div class="shelter-halo"></div>
        <div class="shelter-core">
          <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M8 2l5 2.5v4c0 3-2.5 5.5-5 6.5-2.5-1-5-3.5-5-6.5v-4L8 2z" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </div>
      </div>
      <div class="shelter-badge">
        <span class="shelter-kicker">SAFE HAVEN</span>
        <span class="shelter-name">${escapeHtml(name)}</span>
      </div>
    `;

    el.addEventListener("click", (e) => {
      e.stopPropagation();
      map.flyTo({ center: coords, zoom: Math.max(map.getZoom(), 12.5), duration: 1000 });
    });

    const m = new maplibregl.Marker({ element: el, anchor: "bottom" })
      .setLngLat(coords)
      .addTo(map);

    if (!sheltersVisible || !mapMarkingsVisible) el.style.display = "none";
    shelterMarkers.push(m);
  }
}

function _getFallbackShelters(key) {
  const SHELTER_DEFAULTS = {
    rishiganga: [
      { name: "Joshimath Cantonment Safe Camp", coords: [79.565, 30.556], elev: "2,150 m", type: "High-Ground Military Helipad Camp", cap: "1,200 persons" },
      { name: "Auli High Ridge Relief Station", coords: [79.570, 30.528], elev: "2,800 m", type: "Alpine Evacuation Center", cap: "650 persons" },
      { name: "Tapovan Higher Ground Assembly", coords: [79.638, 30.498], elev: "1,920 m", type: "Community Shelter", cap: "400 persons" },
    ],
    phutkal: [
      { name: "Padum High Plateau Relief Center", coords: [76.885, 33.475], elev: "3,650 m", type: "Zanskar Regional Shelter", cap: "800 persons" },
      { name: "Anmu Village High Terrace Haven", coords: [77.012, 33.310], elev: "3,950 m", type: "Valley High-Terrace Evacuation Point", cap: "250 persons" },
      { name: "Pipiting High Ground Camp", coords: [76.892, 33.488], elev: "3,680 m", type: "High-Ground Emergency Tent Camp", cap: "500 persons" },
    ],
    south_lhonak: [
      { name: "Mangan District Hospital & Relief Camp", coords: [88.528, 27.502], elev: "1,310 m", type: "District Evacuation Haven", cap: "1,500 persons" },
      { name: "Chungthang Upper Ridge Assembly Point", coords: [88.655, 27.615], elev: "1,850 m", type: "Safe Ridge Assembly Haven", cap: "750 persons" },
      { name: "Lachen Higher Ground Monastery Shelter", coords: [88.558, 27.728], elev: "2,750 m", type: "High Altitude Evacuation Haven", cap: "450 persons" },
    ],
    derna: [
      { name: "Al-Fatayeh High Plateau Emergency Camp", coords: [22.685, 32.742], elev: "145 m", type: "Eastern Plateau Disaster Shelter", cap: "3,500 persons" },
      { name: "Derna West Ridge University Campus Haven", coords: [22.610, 32.765], elev: "95 m", type: "Western Escarpment Evacuation Center", cap: "2,200 persons" },
      { name: "Bab Tobruk Safe Haven Zone", coords: [22.645, 32.775], elev: "55 m", type: "High-Ground Urban Relief Station", cap: "1,800 persons" },
    ],
    annamayya: [
      { name: "AP Model School & College Pullampeta (Elevated)", coords: [79.198, 14.149], elev: "225 m", type: "High-Ground Evacuation Center", cap: "1,500 persons" },
      { name: "Rajampeta Government Area Hospital High Ground", coords: [79.157, 14.200], elev: "215 m", type: "Emergency Medical Relief Haven", cap: "2,000 persons" },
      { name: "Nandalur Higher Ground Relief Shelter", coords: [79.120, 14.260], elev: "210 m", type: "Community Disaster Haven", cap: "900 persons" },
    ],
    ivanovo: [
      { name: "Biser High Hill Community Center", coords: [25.865, 41.865], elev: "185 m", type: "Municipal Evacuation Shelter", cap: "450 persons" },
      { name: "Harmanli Regional Emergency Base", coords: [25.905, 41.930], elev: "195 m", type: "Regional Medical & Relief Camp", cap: "1,100 persons" },
    ],
    malpasset: [
      { name: "Frejus High Ridge Disaster Base", coords: [6.745, 43.435], elev: "45 m", type: "High Ground Command Post", cap: "800 persons" },
      { name: "Reyran Valley Upper Plateau Haven", coords: [6.762, 43.515], elev: "165 m", type: "Canyon Rim Evacuation Point", cap: "350 persons" },
    ]
  };

  const list = SHELTER_DEFAULTS[key] || SHELTER_DEFAULTS.annamayya;
  const features = list.map(s => ({
    type: "Feature",
    properties: {
      name: s.name,
      amenity: "shelter",
      elevation: s.elev,
      type_label: s.type,
      capacity: s.cap,
      status: "ACTIVE SAFE HAVEN",
    },
    geometry: {
      type: "Point",
      coordinates: s.coords,
    }
  }));
  return { type: "FeatureCollection", features };
}

let wseVisible = false;
let wseMeta = null;

window.toggleWse = function () {
  wseVisible = !wseVisible;
  const v = wseVisible ? "visible" : "none";
  for (const id of ["wse-fill", "wse-outline",
                    "wse-anchor-halo", "wse-anchor-point", "wse-anchor-label"]) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", v);
  }
  const btn = document.getElementById("btn-toggle-wse");
  if (btn) {
    btn.classList.toggle("is-active", wseVisible);
    btn.setAttribute("aria-pressed", wseVisible ? "true" : "false");
  }
  // Turning on a reconstructed layer says what is and is not evidence in it, once,
  // where the user is looking. The caveat travels with the layer rather than
  // living only in a doc. Two things have to land: the level is reported, the
  // shoreline is not; and the layer stops where the reports stop.
  const note = document.getElementById("wse-note");
  if (note) {
    note.hidden = !wseVisible;
    if (wseVisible && wseMeta) {
      const n = (wseMeta.anchors || []).length;
      const reach = wseMeta.reach_km;
      note.textContent = "Water LEVEL is reported (" + n + " high-water depths, "
        + (wseMeta.acquisition_kind || "DOCUMENTARY") + "). The SHORELINE is not — it is "
        + "read off the same Copernicus GLO-30 the model runs on, so this is not "
        + "independent validation. Covers only the " + (reach != null ? reach + " km" : "reach")
        + " between the outer anchors; the flood went further, the evidence does not.";
    }
  }
};

window.toggleShelters = function () {
  sheltersVisible = !sheltersVisible;
  const v = sheltersVisible ? "visible" : "none";
  if (map.getLayer("shelters-halo")) map.setLayoutProperty("shelters-halo", "visibility", v);
  if (map.getLayer("shelters-point")) map.setLayoutProperty("shelters-point", "visibility", v);
  shelterMarkers.forEach(m => {
    const el = m.getElement();
    if (el) el.style.display = sheltersVisible ? "" : "none";
  });
  const btn = document.getElementById("btn-toggle-shelters");
  if (btn) {
    btn.classList.toggle("is-active", sheltersVisible);
    btn.setAttribute("aria-pressed", sheltersVisible ? "true" : "false");
  }
};

// ── Theme tokens ──────────────────────────────────────────────────────────
function fmtMin(v) {
  return (v === null || v === undefined || Number.isNaN(+v))
    ? "not computed" : `T+${Math.round(+v)}min`;
}
function fmtWin(v) {
  return (v === null || v === undefined || Number.isNaN(+v))
    ? "--" : `${Math.round(+v)}m`;
}

// A value that was not computed is absent, never zero and never "NaN".
// `x || 0` is the single most common way this project's integrity rule gets
// broken: it turns "we could not compute the population at risk" into the
// confident claim "nobody is at risk".
function isMissing(v) {
  return v === null || v === undefined || v === "" || Number.isNaN(+v);
}
function fmtNum(v, dash = "—") {
  return isMissing(v) ? dash : (+v).toLocaleString();
}
function fmtFixed(v, dp = 2, dash = "—") {
  return isMissing(v) ? dash : (+v).toFixed(dp);
}
// OSM place names are third-party text going into innerHTML. Escape them.
function escapeHtml(x) {
  return String(x ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function depthRamp() {
  return [1, 2, 3, 4, 5, 6].map((i) => cssVar(`--depth-${i}`, "#3d7f80"));
}

function priorityRamp() {
  return [cssVar("--accent-sky", "#6fa5a3"),
          cssVar("--accent-cyan", "#1f5f68"),
          cssVar("--accent-rose", "#c4531f")];
}

// ── Road status layer (Phase 1: the differentiator made visible) ────────────
// ── Validation layers: simulated vs observed ──────────────────────────────
// Only the Derna scenario has an observed outcome to compare against. These
// layers stay empty and hidden for every other run rather than rendering an
// empty verdict, because "nothing to compare" and "we matched nothing" look
// identical on a map and are not the same claim.
//
// Colours deliberately avoid red: red is already spoken for by impassable
// roads, and a red flood-verdict next to a red road reads as one category.
const AGREE_COLORS = {
  1: "#14b8a6",   // hit          — simulated and observed
  2: "#a21caf",   // miss         — observed, we did not simulate it
  3: "#eab308",   // false alarm  — we simulated it, it did not happen
};

function _addValidationLayers() {
  map.addSource("agreement", {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  map.addLayer({
    id: "agreement-fill",
    type: "fill",
    source: "agreement",
    layout: { visibility: "none" },
    paint: {
      "fill-color": [
        "match", ["get", "agreement"],
        1, AGREE_COLORS[1],
        2, AGREE_COLORS[2],
        3, AGREE_COLORS[3],
        "#000000",
      ],
      "fill-opacity": 0.55,
    },
  });

  // Observed-anchored flood extent: reported high-water depths added to the
  // sampled channel bed, interpolated along the Cheyyeru and cut against the DEM.
  // This replaced the HAND corridor that used to occupy this slot -- HAND's
  // 2/5/10 m stages were round numbers nobody chose, whereas these levels come
  // off the record. It is STILL a separate source from "observed-extent": the
  // level is reported, the shoreline is our own DEM, so it is not ground truth
  // and must never be mistaken for it. Off by default; the toolbar button and
  // #wse-note carry the caveat.
  //
  // Two features, an envelope from the low and high ends of the reported depth
  // RANGES. The upper bound is emitted first so the tighter lower bound draws on
  // top of it and the doubled fill marks the best-supported core.
  map.addSource("observed-wse", {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  map.addLayer({
    id: "wse-fill",
    type: "fill",
    source: "observed-wse",
    layout: { visibility: "none" },
    paint: {
      "fill-color": [
        "match", ["get", "band"],
        "lo", "#8b3fd1",
        "hi", "#c77dff",
        "#8b3fd1",
      ],
      "fill-opacity": 0.42,
    },
  });
  map.addLayer({
    id: "wse-outline",
    type: "line",
    source: "observed-wse",
    layout: { visibility: "none" },
    paint: {
      "line-color": "#c77dff",
      // the lower bound is the defensible edge, so it gets the heavier stroke
      "line-width": ["case", ["==", ["get", "band"], "lo"], 1.6, 0.7],
      "line-opacity": 0.7,
    },
  });

  // The three reported depths the purple is built from. Drawn ON the layer so it
  // explains itself: the shape exists because these points exist, and it ends
  // where they end. Populated from the geojson's own `metadata.anchors` in
  // _loadContextLayers -- the anchors already ride along with the polygons, so
  // there is nothing extra to fetch.
  map.addSource("wse-anchors", {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  map.addLayer({
    id: "wse-anchor-halo",
    type: "circle",
    source: "wse-anchors",
    layout: { visibility: "none" },
    paint: {
      "circle-radius": 9,
      "circle-color": "#c77dff",
      "circle-opacity": 0.18,
      "circle-stroke-width": 1,
      "circle-stroke-color": "#c77dff",
      "circle-stroke-opacity": 0.45,
    },
  });
  map.addLayer({
    id: "wse-anchor-point",
    type: "circle",
    source: "wse-anchors",
    layout: { visibility: "none" },
    paint: {
      "circle-radius": 4,
      "circle-color": "#f5e9ff",
      "circle-stroke-width": 1.6,
      "circle-stroke-color": "#7b2fbf",
    },
  });
  map.addLayer({
    id: "wse-anchor-label",
    type: "symbol",
    source: "wse-anchors",
    layout: {
      visibility: "none",
      "text-field": ["get", "label"],
      "text-font": ["Noto Sans Regular"],
      "text-size": 10,
      "text-offset": [0, 1.5],
      "text-anchor": "top",
      "text-allow-overlap": false,
    },
    paint: {
      "text-color": "#e9d5ff",
      "text-halo-color": "#1a0b26",
      "text-halo-width": 1.4,
    },
  });

  const wsePopup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, maxWidth: "300px" });
  map.on("mouseenter", "wse-anchor-halo", (e) => {
    map.getCanvas().style.cursor = "help";
    const p = (e.features && e.features[0] && e.features[0].properties) || {};
    wsePopup.setLngLat(e.features[0].geometry.coordinates)
      .setHTML(
        `<div class="village-popup">`
        + `<div class="pop-name">${escapeHtml(p.name || "")}</div>`
        + `<div class="pop-sub" style="font-family:var(--font-mono);font-size:9.5px;color:#c77dff;margin-top:3px;">`
        + `${escapeHtml(p.id || "")} &middot; reported ${p.depth_lo_m}&ndash;${p.depth_hi_m} m</div>`
        + `<div class="pop-sub" style="font-family:var(--font-mono);font-size:9.5px;color:#94a3b8;margin-top:3px;">`
        + `ground ${p.ground_elev_m} m &rarr; water surface ${p.wse_lo_m}&ndash;${p.wse_hi_m} m<br>`
        + `${p.offset_from_stem_m} m off the Cheyyeru, station ${p.station_km} km</div>`
        + `<div class="pop-sub" style="font-size:9px;color:#64748b;margin-top:4px;line-height:1.35;">`
        + `${escapeHtml(p.source || "")}</div>`
        + `</div>`)
      .addTo(map);
  });
  map.on("mouseleave", "wse-anchor-halo", () => {
    map.getCanvas().style.cursor = "";
    wsePopup.remove();
  });

  // The observed extent is drawn with a translucent cyan fill, dark casing, and
  // crisp optic dashed outline on top of everything, so the delineated truth
  // boundary stays unmistakable whichever fill or basemap is active.
  map.addSource("observed-extent", {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  map.addLayer({
    id: "observed-fill",
    type: "fill",
    source: "observed-extent",
    layout: { visibility: "none" },
    paint: {
      "fill-color": "#06b6d4",
      "fill-opacity": 0.22,
    },
  });
  map.addLayer({
    id: "observed-casing",
    type: "line",
    source: "observed-extent",
    layout: { visibility: "none" },
    paint: {
      "line-color": "#083344",
      "line-width": 4.5,
      "line-opacity": 0.75,
    },
  });
  map.addLayer({
    id: "observed-outline",
    type: "line",
    source: "observed-extent",
    layout: { visibility: "none" },
    paint: {
      "line-color": "#22d3ee",
      "line-width": 2.5,
      "line-opacity": 0.95,
      "line-dasharray": [4, 2.5],
    },
  });

  const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false });
  map.on("mouseenter", "agreement-fill", (e) => {
    map.getCanvas().style.cursor = "help";
    popup.setLngLat(e.lngLat)
         .setHTML(`<div class="village-popup"><div class="pop-name">${
           e.features[0].properties.label}</div></div>`)
         .addTo(map);
  });
  map.on("mouseleave", "agreement-fill", () => {
    map.getCanvas().style.cursor = "";
    popup.remove();
  });

  map.on("mouseenter", "observed-fill", (e) => {
    map.getCanvas().style.cursor = "help";
    const p = (e.features && e.features[0] && e.features[0].properties) || {};
    const title = p.event || p.source || "Observed Ground Truth";
    const sub = p.aoi ? `AOI: ${p.aoi}` : (p.source || "Satellite / Field Survey Ground Truth");
    popup.setLngLat(e.lngLat)
         .setHTML(`<div class="village-popup"><div class="pop-name">🛰️ ${escapeHtml(title)}</div><div class="pop-sub" style="font-family:var(--font-mono);font-size:9.5px;color:#22d3ee;margin-top:2px;">${escapeHtml(sub)}</div></div>`)
         .addTo(map);
  });
  map.on("mouseleave", "observed-fill", () => {
    map.getCanvas().style.cursor = "";
    popup.remove();
  });
}

// ── Dam Structure, Crest Axis, and Breach Geometry Layers ────────────────
function _buildDamGeoJSON(dam) {
  if (!dam) return { type: "FeatureCollection", features: [] };
  const features = [];

  // 2. Downstream Outflow Surge Vector
  if (dam.outflow_vector && dam.outflow_vector.length >= 2) {
    features.push({
      type: "Feature",
      properties: {
        layer_type: "outflow_vector",
        label: "BREACH SURGE TRAJECTORY"
      },
      geometry: {
        type: "LineString",
        coordinates: dam.outflow_vector
      }
    });
  }

  // Real DEM/OSM-derived barrier footprint (P1-1) -- only present when the
  // backend geometry validator actually vouches for this scenario.
  if (dam.dam_body_polygon && dam.dam_body_polygon.length >= 3) {
    features.push({
      type: "Feature",
      properties: {
        layer_type: "dam_body",
        label: "BARRIER (DEM-DERIVED)",
      },
      geometry: {
        type: "Polygon",
        coordinates: [dam.dam_body_polygon]
      }
    });
  }

  // 3. Dam Crest Wall Axis Line (Primary Failure Structure)
  if (dam.dam_axis && dam.dam_axis.length >= 2) {
    features.push({
      type: "Feature",
      properties: {
        layer_type: "dam_crest",
        name: dam.name,
        type: dam.type,
        height_m: dam.height,
        crest_len_m: dam.crest_length_m
      },
      geometry: {
        type: "LineString",
        coordinates: dam.dam_axis
      }
    });
  }

  // 3b. Upstream Pre-Dam Crest Wall Axis Line (e.g. Pincha Dam)
  if (dam.predam_axis && dam.predam_axis.length >= 2) {
    features.push({
      type: "Feature",
      properties: {
        layer_type: "dam_crest",
        name: dam.cascade?.upstream_name || "Upstream Pre-Dam",
        type: "Upstream Pre-Dam Embankment",
      },
      geometry: {
        type: "LineString",
        coordinates: dam.predam_axis
      }
    });
  }

  // 3c. Downstream Sub-Dam Crest Wall Axis Line (e.g. Al-Bilad Dam, Chungthang Dam, Tapovan Barrage)
  if (dam.subdam_axis && dam.subdam_axis.length >= 2) {
    features.push({
      type: "Feature",
      properties: {
        layer_type: "dam_crest",
        name: dam.cascade?.downstream_name || "Downstream Sub-Dam",
        type: "Downstream Cascade Structure",
      },
      geometry: {
        type: "LineString",
        coordinates: dam.subdam_axis
      }
    });
  }

  // 4. Breach Notch / Rupture Point (Center of Failure in River Thalweg)
  features.push({
    type: "Feature",
    properties: {
      layer_type: "breach_point",
      name: `Breach: ${dam.name}`,
      mechanism: dam.breach_mode,
      wse: dam.wse,
      q_peak: dam.q_peak,
      hydraulic_ready: !!dam.hydraulic_ready,
      geometry_reason: dam.geometry_reason || null,
    },
    geometry: {
      type: "Point",
      coordinates: [dam.lon, dam.lat]
    }
  });

  // 5. Cascading Surge Reach & Downstream Structure
  if (dam.cascade) {
    const c = dam.cascade;
    if (c.upstream_coords && c.downstream_coords) {
      features.push({
        type: "Feature",
        properties: {
          layer_type: "cascade_reach",
          label: `CASCADING SURGE REACH (${c.distance_km || 0} km)`,
          name: c.type,
        },
        geometry: {
          type: "LineString",
          coordinates: [c.upstream_coords, c.downstream_coords]
        }
      });
    }

    // Upstream Trigger / Pre-Dam point
    if (c.upstream_coords && (Math.abs(c.upstream_coords[0] - dam.lon) > 0.005 || Math.abs(c.upstream_coords[1] - dam.lat) > 0.005)) {
      features.push({
        type: "Feature",
        properties: {
          layer_type: "cascade_point",
          name: c.upstream_name,
          status: c.upstream_status,
        },
        geometry: {
          type: "Point",
          coordinates: c.upstream_coords
        }
      });
    }

    // Midstream Cascade Sub-Dam (e.g. Rishiganga HEP at Reni)
    if (c.subdam_coords) {
      features.push({
        type: "Feature",
        properties: {
          layer_type: "cascade_point",
          name: c.subdam_name,
          status: c.subdam_status,
        },
        geometry: {
          type: "Point",
          coordinates: c.subdam_coords
        }
      });
    }

    // Downstream Cascade Point
    if (c.downstream_coords) {
      features.push({
        type: "Feature",
        properties: {
          layer_type: "cascade_point",
          name: c.downstream_name,
          status: c.downstream_status,
        },
        geometry: {
          type: "Point",
          coordinates: c.downstream_coords
        }
      });
    }
  }

  return { type: "FeatureCollection", features };
}

function _addDamStructureLayers() {
  const currentKey = document.getElementById("scenario-select")?.value || "rishiganga";
  const dam = _getDamData(currentKey) || _getDamData("rishiganga");
  const initialGeo = _buildDamGeoJSON(dam);

  map.addSource("dam-structure", {
    type: "geojson",
    data: initialGeo,
  });

  // 1. Upstream reservoir pool fill, shoreline glow & crisp outline
  map.addLayer({
    id: "dam-pool-fill",
    type: "fill",
    source: "dam-structure",
    filter: ["==", "layer_type", "reservoir_pool"],
    paint: {
      "fill-color": "#0369a1",
      "fill-opacity": 0.65,
    }
  });
  map.addLayer({
    id: "dam-pool-glow",
    type: "line",
    source: "dam-structure",
    filter: ["==", "layer_type", "reservoir_pool"],
    paint: {
      "line-color": "#38bdf8",
      "line-width": 4.5,
      "line-opacity": 0.45,
      "line-blur": 3,
    }
  });
  map.addLayer({
    id: "dam-pool-outline",
    type: "line",
    source: "dam-structure",
    filter: ["==", "layer_type", "reservoir_pool"],
    paint: {
      "line-color": "#7dd3fc",
      "line-width": 1.8,
      "line-opacity": 0.9,
    }
  });

  // 2. Downstream Outflow Surge Vector
  map.addLayer({
    id: "dam-outflow-line",
    type: "line",
    source: "dam-structure",
    filter: ["==", "layer_type", "outflow_vector"],
    paint: {
      "line-color": "#ef4444",
      "line-width": 3,
      "line-dasharray": [2, 1.5],
      "line-opacity": 0.85,
    }
  });

  // 3. Dam Crest Wall Casing (Dark wide barrier wall)
  map.addLayer({
    id: "dam-barrier-casing",
    type: "line",
    source: "dam-structure",
    filter: ["==", "layer_type", "dam_crest"],
    paint: {
      "line-color": "#0b1120",
      "line-width": 8,
      "line-opacity": 0.95,
    }
  });

  // 4. Dam Crest Wall Engineered Line (Structural Amber/Gold)
  map.addLayer({
    id: "dam-barrier-crest",
    type: "line",
    source: "dam-structure",
    filter: ["==", "layer_type", "dam_crest"],
    paint: {
      "line-color": "#f59e0b",
      "line-width": 4.5,
      "line-opacity": 1.0,
    }
  });

  // 5. Dam Breach Point (Glowing rupture notch circle)
  map.addLayer({
    id: "dam-breach-point-casing",
    type: "circle",
    source: "dam-structure",
    filter: ["==", "layer_type", "breach_point"],
    paint: {
      "circle-radius": 8.5,
      "circle-color": "#ef4444",
      "circle-opacity": 0.35,
      "circle-stroke-color": "#ef4444",
      "circle-stroke-width": 1.5,
    }
  });
  map.addLayer({
    id: "dam-breach-point",
    type: "circle",
    source: "dam-structure",
    filter: ["==", "layer_type", "breach_point"],
    paint: {
      "circle-radius": 5,
      "circle-color": "#ffffff",
      "circle-stroke-color": "#ef4444",
      "circle-stroke-width": 2.5,
    }
  });

  // 6. Cascading Surge Reach Line
  map.addLayer({
    id: "dam-cascade-reach-line",
    type: "line",
    source: "dam-structure",
    filter: ["==", "layer_type", "cascade_reach"],
    paint: {
      "line-color": "#f59e0b",
      "line-width": 3.5,
      "line-dasharray": [4, 2],
      "line-opacity": 0.9,
    }
  });

  // 7. Cascading Downstream Structure Point
  map.addLayer({
    id: "dam-cascade-point",
    type: "circle",
    source: "dam-structure",
    filter: ["==", "layer_type", "cascade_point"],
    paint: {
      "circle-radius": 7,
      "circle-color": "#f59e0b",
      "circle-stroke-color": "#ffffff",
      "circle-stroke-width": 2,
    }
  });

  // Clicks and Hovers
  map.on("mouseenter", "dam-barrier-crest", () => { map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", "dam-barrier-crest", () => { map.getCanvas().style.cursor = ""; });
  map.on("click", "dam-barrier-crest", () => {
    const curKey = document.getElementById("scenario-select")?.value || "rishiganga";
    window.focusDamBreachPoint(curKey);
  });
  map.on("mouseenter", "dam-breach-point", () => { map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", "dam-breach-point", () => { map.getCanvas().style.cursor = ""; });
  map.on("click", "dam-breach-point", () => {
    const curKey = document.getElementById("scenario-select")?.value || "rishiganga";
    window.focusDamBreachPoint(curKey);
  });
}

function _updateDamStructure(scenarioKey) {
  if (!map || !map.getSource("dam-structure")) return;
  const key = scenarioKey || document.getElementById("scenario-select")?.value || "rishiganga";
  const dam = _getDamData(key) || _getDamData("rishiganga");
  const geo = _buildDamGeoJSON(dam);
  map.getSource("dam-structure").setData(geo);
}

function _updateDamSetupCard(scenarioKey) {
  const key = scenarioKey || document.getElementById("scenario-select")?.value || "rishiganga";
  const dam = _getDamData(key) || _getDamData("rishiganga");
  if (!dam) return;

  const tTitle = document.getElementById("dam-card-title");
  const tDesc = document.getElementById("dam-card-desc");
  const tCoords = document.getElementById("dam-card-coords");
  const tHeight = document.getElementById("dam-card-height");
  const tVol = document.getElementById("dam-card-vol");
  const tFailure = document.getElementById("dam-card-failure");

  if (tTitle) tTitle.textContent = dam.name;
  if (tDesc) tDesc.textContent = dam.desc || dam.event || dam.type;
  if (tCoords) tCoords.textContent = `${dam.lat.toFixed(4)}°N, ${dam.lon.toFixed(4)}°E`;
  if (tHeight) tHeight.textContent = `${dam.height} m · WSE ${dam.wse} m`;
  if (tVol) tVol.textContent = `${dam.vol} MCM`;
  if (tFailure) tFailure.textContent = dam.breach_short || dam.breach_mode || "Breach";

  // Geometry notice removed for presentation
  const existingNotice = document.getElementById("dam-card-geometry-notice");
  if (existingNotice) existingNotice.remove();
}

// Short-lived visual cues on the existing dam marker: a pulse while
// overtopping (T-45..T+0) and a one-shot flash at the breach (T+0). Per
// ui.md's animation rule, the marker itself is always visible without either
// class -- these only ADD emphasis, never provide the only visibility.
let _breachFlashPlayed = false;
function _updateDamOvertopBreachFx(tMin) {
  if (!damMarker) return;
  const el = damMarker.getElement();
  if (!el) return;
  const isOvertop = tMin >= -45 && tMin < 0;
  el.classList.toggle("is-overtop", isOvertop);

  if (tMin >= 0 && !_breachFlashPlayed) {
    _breachFlashPlayed = true;
    el.classList.remove("is-breach-flash");
    void el.offsetWidth;
    el.classList.add("is-breach-flash");
  } else if (tMin < 0 && _breachFlashPlayed) {
    // Scrubbed back before the breach -- allow the flash to replay if T+0 is
    // crossed again.
    _breachFlashPlayed = false;
    el.classList.remove("is-breach-flash");
  }
}

function setValidationVisible(on) {
  const v = on ? "visible" : "none";
  for (const id of ["agreement-fill", "observed-fill", "observed-casing", "observed-outline"]) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", v);
  }
  if (observedMarker) {
    const el = observedMarker.getElement();
    if (el) el.style.display = on ? "" : "none";
  }
  // The agreement overlay and the animated depth layer occupy the same pixels.
  // Showing both stacks two translucent fills and neither is readable, so the
  // depth layer steps aside while the verdict is up.
  if (map.getLayer("flood-depth-fill")) {
    map.setPaintProperty("flood-depth-fill", "fill-opacity", 0);
  }
  if (map.getLayer("flood-raster-fill")) {
    map.setPaintProperty("flood-raster-fill", "raster-opacity", on ? 0.18 : 0.82);
  }
  if (map.getLayer("flood-depth-next-fill")) {
    map.setPaintProperty("flood-depth-next-fill", "fill-opacity", 0);
  }
}

function _addRoadLayer() {
  map.addSource("roads-timeline", {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });

  map.addLayer({
    id: "roads-open",
    type: "line",
    source: "roads-timeline",
    // "Never cut" is carried two ways: the key absent (older emitters) or the
    // key present with null (emit_road_cut_timeline writes every link, null
    // when it never goes under). Testing only `has` put all 5,299 Annamayya
    // links in roads-cut-soon, where a null in the paint `<=` fails and
    // MapLibre falls back to black -- a black mesh over the whole valley.
    filter: ["!", _HAS_CUT_TIME],
    // Faint on purpose: Annamayya has 5,299 links and only 355 are ever cut.
    // At valley zoom a solid 1.5 px grey turned the whole map into a black mesh
    // that buried the flood; the links that matter are drawn by roads-cut-soon.
    paint: {
      "line-color": "#94a3b8",
      "line-width": ["interpolate", ["linear"], ["zoom"], 9, 0.3, 12, 0.9, 15, 2],
      "line-opacity": 0.45,
    },
  });

  // One layer for every link that gets cut, with a STATIC filter. Which of
  // them are already impassable at the scrubber's time is expressed in paint,
  // not in the filter.
  //
  // This used to be two layers whose filters were rewritten on every frame.
  // setFilter re-runs the filter over every feature and re-tiles the source;
  // on the Derna graph that is 10,591 line features re-tiled twice per 600 ms
  // tick, which is what made the map crawl during playback. Paint properties
  // update without re-tiling, so the per-frame cost stops scaling with the
  // size of the road network.
  map.addLayer({
    id: "roads-cut-soon",
    type: "line",
    source: "roads-timeline",
    filter: _HAS_CUT_TIME,
    paint: {
      "line-color": _roadColorAt(0),
      "line-width": _roadWidthAt(0),
      "line-opacity": _roadOpacityAt(0),
    },
  });

  map.addLayer({
    id: "roads-bridge-cut",
    type: "line",
    source: "roads-timeline",
    filter: ["all",
      ["==", ["get", "is_bridge"], true],
      _HAS_CUT_TIME,
    ],
    paint: {
      "line-color": "#7c3aed",
      "line-width": 4,
      // Time-aware like the other cut layers. Previously a bridge that goes
      // under at T+50 was drawn purple from T+0, so the map showed bridges as
      // lost before the water reached them.
      "line-opacity": _bridgeOpacityAt(0),
    },
  });
}

function _bridgeOpacityAt(tMin) {
  if (tMin < 0) return 0;
  return ["case", ["<=", ["get", "cut_time_min"], tMin], 0.9, 0];
}

// Already cut at time t -> red and thick. Not yet cut -> amber and thin.
// Expressed as data-driven paint so the per-frame update is a paint swap
// rather than a re-tile of the whole road source.
// Three states at the scrubber's minute: already cut (red), cut within the
// next ROAD_WARN_MIN (orange, "about to go"), and everything later, which is
// drawn like an open road. Painting every eventually-cut link orange from T+0
// showed 355 warnings before any water had moved.
const ROAD_WARN_MIN = 60;
// True only for links with a real cut time -- see the roads-open filter.
const _HAS_CUT_TIME = ["all", ["has", "cut_time_min"], ["!=", ["get", "cut_time_min"], null]];
function _roadColorAt(tMin) {
  if (tMin < 0) return "#94a3b8";
  return ["case",
    ["<=", ["get", "cut_time_min"], tMin], "#dc2626",
    ["<=", ["get", "cut_time_min"], tMin + ROAD_WARN_MIN], "#f97316",
    "#94a3b8"];
}
function _roadWidthAt(tMin) {
  const cut = tMin < 0 ? false : ["<=", ["get", "cut_time_min"], tMin];
  const warn = tMin < 0 ? false : ["<=", ["get", "cut_time_min"], tMin + ROAD_WARN_MIN];
  return ["interpolate", ["linear"], ["zoom"],
    9,  ["case", cut, 1.6, warn, 1.2, 0.3],
    13, ["case", cut, 4,   warn, 2.4, 0.9]];
}
function _roadOpacityAt(tMin) {
  if (tMin < 0) return 0.45;
  return ["case", ["<=", ["get", "cut_time_min"], tMin + ROAD_WARN_MIN], 0.95, 0.45];
}

let _lastRoadT = null;
function _updateRoadLayerAtTime(tMin) {
  if (!map.getLayer("roads-cut-soon")) return;
  // The scrubber fires on every drag pixel; only repaint when the minute the
  // styling actually depends on has changed.
  const t = Math.round(tMin);
  if (t === _lastRoadT) return;
  _lastRoadT = t;
  map.setPaintProperty("roads-cut-soon", "line-color", _roadColorAt(t));
  map.setPaintProperty("roads-cut-soon", "line-width", _roadWidthAt(t));
  map.setPaintProperty("roads-cut-soon", "line-opacity", _roadOpacityAt(t));
  if (map.getLayer("roads-bridge-cut")) {
    map.setPaintProperty("roads-bridge-cut", "line-opacity", _bridgeOpacityAt(t));
  }
}

// ── Rivers layer ──────────────────────────────────────────────────────────
function _addRiversLayer() {
  map.addSource("rivers", {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  map.addLayer({
    id: "rivers-line",
    type: "line",
    source: "rivers",
    paint: {
      "line-color": "#38bdf8",
      "line-width": 1.8,
      "line-opacity": 0.75,
    },
  });
}

// ── Buildings layer ──────────────────────────────────────────────────
function _addBuildingsLayer() {
  map.addSource("buildings", {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  map.addLayer({
    id: "buildings-fill",
    type: "fill",
    source: "buildings",
    paint: {
      "fill-color": "#78716c",
      "fill-opacity": 0.6,
    },
  });
  map.addLayer({
    id: "buildings-outline",
    type: "line",
    source: "buildings",
    paint: {
      "line-color": "#44403c",
      "line-width": 0.8,
    },
  });
}

// ── Flood layer ─────────────────────────────────────────────────────
function _addFloodLayer() {
  map.addSource("flood-depth", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addSource("flood-depth-next", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addSource("flood-raster", {
    type: "image",
    url: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/6R5sWQAAAABJRU5ErkJggg==",
    coordinates: [[-180, 85], [180, 85], [180, -85], [-180, -85]],
  });
  map.addLayer({
    id: "flood-raster-fill",
    type: "raster",
    source: "flood-raster",
    paint: { "raster-opacity": 0.82, "raster-resampling": "linear" },
  });

  // D8 FIX: corrected ramp step indices. Previously index 3 (--depth-4)
  // was dead, producing a non-monotonic ramp with a gap at class 3.
  // depthRamp() = [--depth-1, --depth-2, --depth-3, --depth-4, --depth-5, --depth-6]
  //   class 1: 0.3-1m   → depthRamp()[0]  (lightest)
  //   class 2: 1-2m     → depthRamp()[1]
  //   class 3: 2-3m     → depthRamp()[3]  (was [2] before — [3] is --depth-4)
  //   class 4: >3m      → depthRamp()[5]  (darkest, most dangerous)
  map.addLayer({
    id: "flood-depth-fill",
    type: "fill",
    source: "flood-depth",
    paint: {
      "fill-color": [
        "step", ["get", "depth_class"],
        depthRamp()[0],      // default (class 0, safety value)
        1, depthRamp()[0],   // 0.3 – 1 m  (light)
        2, depthRamp()[1],   // 1 – 2 m    (medium)
        3, depthRamp()[3],   // 2 – 3 m    (deep)
        4, depthRamp()[5],   // > 3 m      (darkest)
      ],
      "fill-opacity": 0,
    },
  });

  map.addLayer({
    id: "flood-depth-next-fill",
    type: "fill",
    source: "flood-depth-next",
    paint: {
      "fill-color": [
        "step", ["get", "depth_class"],
        depthRamp()[0], 1, depthRamp()[0], 2, depthRamp()[1],
        3, depthRamp()[3], 4, depthRamp()[5],
      ],
      "fill-opacity": 0,
    },
  });

  map.addLayer({
    id: "flood-depth-glow",
    type: "line",
    source: "flood-depth-next",
    layout: { "line-join": "round", "line-cap": "round" },
    paint: {
      "line-color": cssVar("--accent-cyan", "#0ea5e9"),
      "line-width": 7,
      "line-opacity": 0.12,
      "line-blur": 3,
    },
  });

  // D9 FIX: removed hardcoded '#67e8f9' and replaced with theme-aware color.
  map.addLayer({
    id: "flood-depth-outline",
    type: "line",
    source: "flood-depth",
    paint: {
      "line-color": cssVar("--accent-cyan", "#0ea5e9"),
      "line-width": 1.0,
      "line-opacity": 0.32,
    },
    layout: { "line-join": "round", "line-cap": "round" },
  });
}

// ── Lake layer (run_layer/lake_frames) ─────────────────────────────────────
// One polygon per solver step: 36 rising frames sourced pre-breach, 27
// draining frames from a 0-D mass balance post-breach. Drawn by STEP (the
// frame with the greatest t_min <= current t, no interpolation) so the shape
// on screen always matches a real solver frame instead of an invented
// in-between one.
//
// Deliberately its own colour, never the flood-depth ramp and never the
// chrome accent (ui.md): a reservoir is not the same claim as flooded land,
// and must not be readable as either.
function _addLakeLayer() {
  map.addSource("lake", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: "lake-fill",
    type: "fill",
    source: "lake",
    paint: {
      "fill-color": "#1e3a8a",
      "fill-opacity": 0.8,
    },
  });
  map.addLayer({
    id: "lake-shoreline",
    type: "line",
    source: "lake",
    paint: {
      "line-color": "#7dd3fc",
      "line-width": 1.4,
      "line-opacity": 0.85,
    },
  });

  lakePopup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, maxWidth: "280px" });
  map.on("mouseenter", "lake-fill", (e) => {
    map.getCanvas().style.cursor = "pointer";
    const p = (e.features[0] && e.features[0].properties) || {};
    lakePopup.setLngLat(e.lngLat).setHTML(`
      <div class="village-popup">
        <div class="pop-name">Reservoir</div>
        <dl class="pop-grid" style="margin-top:6px;">
          <dt>Level</dt><dd>${isMissing(p.level_m) ? "—" : (+p.level_m).toFixed(1) + " m"}</dd>
          <dt>Volume</dt><dd>${isMissing(p.volume_mcm) ? "—" : (+p.volume_mcm).toFixed(2) + " MCM"}</dd>
          <dt>Area</dt><dd>${isMissing(p.area_km2) ? "—" : (+p.area_km2).toFixed(2) + " km²"}</dd>
          <dt>Source</dt><dd>${escapeHtml(p.source || "—")}</dd>
        </dl>
      </div>
    `).addTo(map);
  });
  map.on("mouseleave", "lake-fill", () => {
    map.getCanvas().style.cursor = "";
    lakePopup.remove();
  });
}

// Step lookup: the frame with the greatest t_min <= t. Before the first
// frame, nothing is drawn -- there is no real solver state to show yet.
function _lakeFrameAt(tMin) {
  if (!lakeFrames.length) return null;
  let best = null;
  for (const f of lakeFrames) {
    if (f.t_min <= tMin && (!best || f.t_min > best.t_min)) best = f;
  }
  return best;
}

let _lastLakeT = null;
function _updateLakeAtTime(tMin) {
  if (!map || !map.getSource("lake")) return;
  const t = Math.round(tMin);
  if (t === _lastLakeT) return;
  _lastLakeT = t;
  const frame = _lakeFrameAt(tMin);
  map.getSource("lake").setData(frame ? frame.geojson : { type: "FeatureCollection", features: [] });
}

async function _loadLakeFrames(jobId) {
  lakeFrames = [];
  _lastLakeT = null;
  try {
    const r = await fetch(`/api/run_layer/${jobId}/lake_frames`);
    if (!r.ok) return;
    const fc = await r.json();
    lakeFrames = (fc.features || [])
      .map((f) => {
        const p = f.properties || {};
        return {
          t_min: +p.t_min,
          level_m: p.level_m, volume_mcm: p.volume_mcm, area_km2: p.area_km2,
          phase: p.phase, source: p.source,
          geojson: { type: "FeatureCollection", features: [f] },
        };
      })
      .filter((f) => isFinite(f.t_min))
      .sort((a, b) => a.t_min - b.t_min);
  } catch (e) {
    console.warn("lake_frames not available:", e);
  }
}

// ── Existing water planes (run_layer/water_planes) ──────────────────────────
// Flat water surfaces already in the DEM (e.g. Somasila reservoir), drawn
// ABOVE the flood layers and the lake so flood water that ponds on an
// existing water body never reads as newly-flooded land.
function _addWaterPlanesLayer() {
  map.addSource("water-planes", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: "water-planes-fill",
    type: "fill",
    source: "water-planes",
    paint: {
      "fill-color": "#9cc3e6",
      "fill-opacity": 0.92,
    },
  });
  map.addLayer({
    id: "water-planes-outline",
    type: "line",
    source: "water-planes",
    paint: {
      "line-color": "#5b86ad",
      "line-width": 0.8,
      "line-opacity": 0.8,
    },
  });

  waterPlanesPopup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, maxWidth: "280px" });
  map.on("mouseenter", "water-planes-fill", (e) => {
    map.getCanvas().style.cursor = "pointer";
    const p = (e.features[0] && e.features[0].properties) || {};
    const elev = isMissing(p.elev_m) ? "—" : (+p.elev_m).toFixed(1);
    waterPlanesPopup.setLngLat(e.lngLat).setHTML(`
      <div class="village-popup">
        <div class="pop-name">Existing water surface</div>
        <p style="margin:4px 0 0;font-size:11px;color:var(--text-secondary);">
          Existing water surface in the DEM (elev ${elev} m). Flood water that ponds on it
          is not drawn as flooded land.
        </p>
      </div>
    `).addTo(map);
  });
  map.on("mouseleave", "water-planes-fill", () => {
    map.getCanvas().style.cursor = "";
    waterPlanesPopup.remove();
  });
}

async function _loadWaterPlanes(jobId) {
  try {
    const r = await fetch(`/api/run_layer/${jobId}/water_planes`);
    const src = map.getSource("water-planes");
    if (!r.ok || !src) { if (src) src.setData({ type: "FeatureCollection", features: [] }); return; }
    const fc = await r.json();
    src.setData(fc);
  } catch (e) {
    console.warn("water_planes not available:", e);
  }
}

// ── Context layers, per scenario ──────────────────────────────────────────
// These used to point at "assets/phutkal_*.geojson": a 404 on every load, and
// hard-wired to Phutkal so any other scenario would have drawn the wrong
// country's rivers. Fetched per scenario instead; a missing file is served as
// an empty collection, so a layer with nothing in it renders as nothing rather
// than failing.
async function _loadContextLayers(scenarioKey) {
  for (const [kind, srcId] of [["rivers", "rivers"], ["buildings", "buildings"], ["villages", "villages"], ["observed_wse", "observed-wse"]]) {
    try {
      const r = await fetch(`/api/layers/${scenarioKey}/${kind}`);
      if (!r.ok) continue;
      const data = await r.json();
      const src = map.getSource(srcId);
      if (src) src.setData(data);
      if (kind === "villages" && (!simulationResults || !simulationResults.features || !simulationResults.features.length)) {
        _updateVillageMarkers(data);
      }
      if (kind === "observed_wse") {
        // The button is disabled for scenarios with no reconstruction on disk, so
        // an enabled-but-empty control can never suggest "no flooding here".
        wseMeta = (data && data.metadata) || null;
        const n = (data && data.features && data.features.length) || 0;
        // The anchors are already in the payload; turn them into points rather
        // than fetching them a second time from somewhere else.
        const asrc = map.getSource("wse-anchors");
        if (asrc) {
          asrc.setData({
            type: "FeatureCollection",
            features: ((wseMeta && wseMeta.anchors) || []).map((a) => ({
              type: "Feature",
              properties: {
                id: a.id,
                name: a.name,
                label: `${a.name} · ${a.depth_lo_m}–${a.depth_hi_m} m`,
                depth_lo_m: a.depth_lo_m,
                depth_hi_m: a.depth_hi_m,
                ground_elev_m: (a.ground_elev_m != null) ? a.ground_elev_m.toFixed(1) : "",
                wse_lo_m: (a.wse_lo_m != null) ? a.wse_lo_m.toFixed(1) : "",
                wse_hi_m: (a.wse_hi_m != null) ? a.wse_hi_m.toFixed(1) : "",
                offset_from_stem_m: (a.offset_from_stem_m != null) ? Math.round(a.offset_from_stem_m) : "",
                station_km: (a.station_m != null) ? (a.station_m / 1000).toFixed(2) : "",
                source: a.source,
              },
              geometry: { type: "Point", coordinates: [a.lon, a.lat] },
            })),
          });
        }
        const btn = document.getElementById("btn-toggle-wse");
        if (btn) {
          btn.disabled = n === 0;
          btn.title = n === 0
            ? "No reported high-water depths for this scenario"
            : "Extent from REPORTED high-water depths — level is observed, shoreline is DEM. Not ground truth, no skill score.";
        }
        if (n === 0 && wseVisible) window.toggleWse();
      }
    } catch (e) {
      console.warn(`context layer ${kind} unavailable:`, e);
    }
  }
  _updateDamMarker(scenarioKey);

  // Load scenario's observed ground truth footprint (if wired)
  try {
    const rObs = await fetch(`/api/layers/${scenarioKey}/observed`);
    if (rObs.ok) {
      const obsGeo = await rObs.json();
      currentObservedGeoJSON = obsGeo;
      if (map.getSource("observed-extent")) {
        map.getSource("observed-extent").setData(obsGeo);
      }
      for (const id of ["observed-fill", "observed-casing", "observed-outline"]) {
        if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", "visible");
      }
      const obsMeta = await fetch(`/api/observed/scenarios`).then(r => r.ok ? r.json() : {}).catch(() => ({}));
      _updateObservedMarker(obsGeo, obsMeta[scenarioKey] || null);
    } else {
      currentObservedGeoJSON = null;
      if (map.getSource("observed-extent")) {
        map.getSource("observed-extent").setData({ type: "FeatureCollection", features: [] });
      }
      for (const id of ["observed-fill", "observed-casing", "observed-outline"]) {
        if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", "none");
      }
      if (observedMarker) {
        observedMarker.remove();
        observedMarker = null;
      }
    }
  } catch (e) {
    console.warn("observed layer unavailable:", e);
  }


  // Load shelters & relief facilities
  try {
    const rFac = await fetch(`/api/layers/${scenarioKey}/facilities`);
    let facGeo = rFac.ok ? await rFac.json() : null;
    if (!facGeo || !facGeo.features || !facGeo.features.length) {
      facGeo = _getFallbackShelters(scenarioKey);
    }
    if (map.getSource("shelters")) map.getSource("shelters").setData(facGeo);
    _updateShelterMarkers(facGeo, scenarioKey);
  } catch (e) {
    console.warn("facilities layer unavailable:", e);
    const fallback = _getFallbackShelters(scenarioKey);
    if (map.getSource("shelters")) map.getSource("shelters").setData(fallback);
    _updateShelterMarkers(fallback, scenarioKey);
  }
}

// ── Basemap switcher ───────────────────────────────────────────────
function switchBasemap(mode) {
  if (!map || !map.isStyleLoaded()) return;
  currentBasemap = mode;
  const src = map.getSource("osm");
  if (!src) return;

  const urls = BASEMAP_URLS[mode] || BASEMAP_URLS.streets;
  src.setTiles(urls);
  _applyBasemapPaint(mode);

  document.querySelectorAll("#basemap-switcher .seg").forEach(b => {
    b.classList.toggle("is-active", b.dataset.mode === mode);
    b.setAttribute("aria-pressed", b.dataset.mode === mode ? "true" : "false");
  });
}

// Basemap paint lives here so switchBasemap() and the theme handler cannot
// disagree. They used to set these three properties independently: picking
// Satellite and then toggling the theme ran the theme's branch, which
// desaturated and dimmed the imagery as though it were the streets basemap.
// `currentBasemap` was assigned by switchBasemap and then never read anywhere.
function _applyBasemapPaint(mode) {
  if (!map || !map.getLayer("osm-tiles")) return;
  const isDark = mode === "dark";
  const isSat  = mode === "satellite";
  map.setPaintProperty("osm-tiles", "raster-saturation",
    isSat ? 0 : isDark ? -0.55 : -0.25);
  map.setPaintProperty("osm-tiles", "raster-brightness-max",
    isDark ? 0.55 : 1.0);
  map.setPaintProperty("osm-tiles", "raster-brightness-min", isDark ? 0.0 : 0.06);
  map.setPaintProperty("osm-tiles", "raster-opacity",
    isDark ? 0.7 : isSat ? 0.95 : 0.9);
}

// ── Envelope layer ──────────────────────────────────────────────────────────
function _addEnvelopeLayer() {
  map.addSource("envelope", {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] }
  });

  map.addLayer({
    id: "envelope-fill",
    type: "fill",
    source: "envelope",
    paint: {
      "fill-color": "#fb923c",
      "fill-opacity": 0.15,
    },
  });

  map.addLayer({
    id: "envelope-outline",
    type: "line",
    source: "envelope",
    paint: {
      "line-color": "#fb923c",
      "line-width": 1.5,
      "line-opacity": 0.8,
      "line-dasharray": [4, 2],
    },
  });
}

// ── Helper: update the flood layer with a new GeoJSON frame ───────────────
function _setFloodFrame(geojsonData) {
  if (map.getSource("flood-depth")) {
    map.getSource("flood-depth").setData(geojsonData);
  }
}

function _transitionToFloodFrame(frame) {
  const current = map.getSource("flood-depth");
  if (!current || !frame) return;

  const transition = ++frameTransitionId;
  const raster = map.getSource("flood-raster");
  const hasRaster = !!(raster && frame.raster_url && frame.preview_bounds && frame.stage !== "reservoir_rise");

  if (hasRaster) {
    raster.updateImage({ url: frame.raster_url, coordinates: frame.preview_bounds });
    if (map.getLayer("flood-raster-fill")) {
      map.setPaintProperty("flood-raster-fill", "raster-opacity", 0.82);
    }
    if (map.getLayer("flood-depth-fill")) {
      map.setPaintProperty("flood-depth-fill", "fill-opacity", 0);
    }
    if (map.getLayer("flood-depth-next-fill")) {
      map.setPaintProperty("flood-depth-next-fill", "fill-opacity", 0);
    }
    if (map.getLayer("flood-depth-glow")) {
      map.setPaintProperty("flood-depth-glow", "line-opacity", 0.12);
    }
    if (frame.geojson) current.setData(frame.geojson);
    return;
  }

  // Vector fallback or reservoir rise
  if (map.getLayer("flood-raster-fill")) {
    map.setPaintProperty("flood-raster-fill", "raster-opacity", 0);
  }
  if (frame.geojson) current.setData(frame.geojson);
  if (map.getLayer("flood-depth-fill")) {
    if (frame.stage === "reservoir_rise") {
      // The lake layer (run_layer/lake_frames) draws the reservoir for this
      // stage when it has data; painting flood-depth-fill too would double
      // the same water in two layers. Fall back to the old solid fill only
      // when this run has no lake_frames of its own.
      if (lakeFrames.length) {
        map.setPaintProperty("flood-depth-fill", "fill-opacity", 0);
      } else {
        map.setPaintProperty("flood-depth-fill", "fill-color", "#0284c7");
        map.setPaintProperty("flood-depth-fill", "fill-opacity", 0.68);
      }
    } else {
      map.setPaintProperty("flood-depth-fill", "fill-color", [
        "step", ["get", "depth_class"],
        depthRamp()[0], 1, depthRamp()[0], 2, depthRamp()[1],
        3, depthRamp()[3], 4, depthRamp()[5],
      ]);
      map.setPaintProperty("flood-depth-fill", "fill-opacity", 0.72);
    }
  }
  if (map.getLayer("flood-depth-next-fill")) {
    map.setPaintProperty("flood-depth-next-fill", "fill-opacity", 0);
  }
  if (map.getLayer("flood-depth-glow")) {
    map.setPaintProperty("flood-depth-glow", "line-opacity", 0.12);
  }
}

// ── Async: load snapshot frames after run completes ───────────────────────
// D15 FIX: replaced serial for-loop with Promise.all so all frame fetches
// are issued in parallel. Reduces time-to-first-frame from ~13 round-trips
// in series to ~1 round-trip worth of latency.
async function _loadSnapshotFrames(jobId) {
  try {
    const resp = await fetch(`/api/snapshots/${jobId}`);
    if (!resp.ok) {
      const errText = await resp.text();
      console.warn(`Snapshots endpoint returned ${resp.status}: ${errText}`);
      return;
    }
    const data = await resp.json();
    const frames = data.frames || [];
    
    if (!Array.isArray(frames) || frames.length === 0) {
      console.warn("No frames in snapshot response:", data);
      return;
    }

    console.log(`Loading ${frames.length} snapshot frames for job ${jobId}`);

    // Fetch all GeoJSON frames in parallel
    const fetched = await Promise.all(
      frames.map((fr, i) =>
        fetch(`/api/snapshots/${jobId}/frame/${i}`)
          .then(r => {
            if (!r.ok) {
              console.warn(`Frame ${i} fetch failed: ${r.status}`);
              return null;
            }
            return r.json();
          })
          .then(geojson => {
            if (!geojson) return null;
            const t_min = fr.t_min !== undefined ? fr.t_min : 0;
            return {
              t_min, geojson,
              raster_url: `/api/snapshots/${jobId}/frame/${i}/raster`,
              preview_bounds: fr.preview_bounds,
              stage: fr.stage,
              phase_title: fr.phase_title,
              volume_mcm: fr.volume_mcm,
              area_km2: fr.area_km2,
              level_m: fr.level_m,
              // Pre-breach inflow. Without it the discharge curve starts at
              // T=0 and the whole lake-formation half of the timeline reads as
              // "no water moving", which is the opposite of what happened.
              inflow_m3s: fr.inflow_m3s,
            };
          })
          .catch(e => {
            console.warn(`Frame ${i} parse error:`, e);
            return null;
          })
      )
    );
    
    snapshotFrames = fetched.filter(Boolean);
    console.log(`Successfully loaded ${snapshotFrames.length} / ${frames.length} snapshot frames`);

    // Idle-prefetch PNG raster images so timeline dragging has zero network latency
    const prefetchImages = () => {
      snapshotFrames.forEach(f => {
        if (f.raster_url) {
          const img = new Image();
          img.src = f.raster_url;
        }
      });
    };
    if (typeof requestIdleCallback === "function") {
      requestIdleCallback(prefetchImages);
    } else {
      setTimeout(prefetchImages, 100);
    }

    // Precompute wavefront data for all frames at load time
    const curKey = document.getElementById("scenario-select")?.value || "annamayya";
    const damInfo = _getDamData(curKey) || _getDamData("annamayya");
    snapshotFrames.forEach(frame => {
      if (!frame._wavefrontData && frame.geojson && frame.geojson.features) {
        let maxDist = 0;
        let wavePt = null;
        let maxDepth = 0;
        for (const f of frame.geojson.features) {
          const dClass = f.properties?.depth_class || 1;
          const dM = f.properties?.depth_lo_m || (dClass * 0.5);
          if (dM > maxDepth) maxDepth = dM;
          const coords = f.geometry?.coordinates;
          if (!coords) continue;
          const testRing = (ring) => {
            const step = ring.length > 80 ? 3 : 1;
            for (let i = 0; i < ring.length; i += step) {
              const pt = ring[i];
              const d = Math.hypot(pt[0] - damInfo.lon, pt[1] - damInfo.lat);
              if (d > maxDist) {
                maxDist = d;
                wavePt = pt;
              }
            }
          };
          if (f.geometry.type === "Polygon") {
            testRing(coords[0]);
          } else if (f.geometry.type === "MultiPolygon") {
            for (const poly of coords) testRing(poly[0]);
          }
        }
        frame._wavefrontData = { wavePt, maxDist, maxDepth };
      }
    });

    if (snapshotFrames.length > 0) {
      // The range input is the timeline's only input surface: it lies
      // transparently over the lanes, so drag, arrow keys, Home/End and screen
      // readers all work without any pointer handling of our own.
      const slider = document.getElementById("time-slider");
      if (slider) {
        slider.max = snapshotFrames.length - 1;
        slider.value = 0;
      }

      // The timeline's domain is the frame series, so it is set from here.
      // Observations are loaded here, not from the validation panel: that
      // function returns early when `#validation-panel` is absent, so the
      // ticker silently had no records to compare against.
      _loadVillageObservations(jobId);

      // Warm the browser cache with every frame's preview PNG. Playback swaps
      // the raster source as fast as one frame per 70 ms; an uncached PNG has
      // to be fetched AND decoded inside that budget, and losing the race is
      // what raised "InvalidStateError: The source image could not be decoded"
      // and left the map on a stale frame. The responses are already
      // `immutable, max-age=86400`, so this costs one pass and nothing after.
      // Fire-and-forget: a failed preload just means the old behaviour.
      snapshotFrames.forEach((fr) => {
        if (!fr || !fr.raster_url) return;
        const img = new Image();
        img.decoding = "async";
        img.src = fr.raster_url;
      });

      if (window.Spine) {
        window.Spine.setDomain(snapshotFrames);
        // Pre-breach frames carry level_m; the stage lane draws itself from
        // whichever frames have it, so a run without a lake-formation stage
        // simply shows no stage curve rather than erroring.
        window.Spine.setStage(snapshotFrames);
      }

      _lastAppliedIdx = -1;
      _applySnapshotFrame(0);

      const playBtn = document.getElementById("btn-play");
      if (playBtn) playBtn.hidden = false;

      // Log to debug panel
      if (window.FloodSightDebug) {
        window.FloodSightDebug.log.push({
          t: +(performance.now() / 1000).toFixed(3),
          level: "info",
          tag: "snapshots",
          msg: `Loaded ${snapshotFrames.length} frames, duration T+0..T+${Math.round(snapshotFrames[snapshotFrames.length-1].t_min)}min`,
          extra: { frame_count: snapshotFrames.length },
        });
      }
    } else {
      console.warn("No frames successfully loaded for playback");
    }
  } catch (e) {
    console.error("Snapshots load failed:", e);
    if (window.FloodSightDebug) {
      window.FloodSightDebug.log.push({
        t: +(performance.now() / 1000).toFixed(3),
        level: "error",
        tag: "snapshots",
        msg: "Snapshot frames failed to load: " + e.message,
        extra: { error: String(e) },
      });
    }
  }
}

let _lastAppliedIdx = -1;

function _applySnapshotFrame(idx) {
  if (idx === currentStep && _lastAppliedIdx === idx) return;
  _lastAppliedIdx = idx;
  currentStep = idx;
  const frame = snapshotFrames[idx];
  if (!frame) return;

  if (!playTimer) {
    // Instant frame display during manual scrubbing for zero-lag response
    frameTransitionId++;
    const current = map.getSource("flood-depth");
    if (current && frame.geojson) current.setData(frame.geojson);
    const raster = map.getSource("flood-raster");
    const hasRaster = !!(raster && frame.raster_url && frame.preview_bounds && frame.stage !== "reservoir_rise");

    if (hasRaster) {
      raster.updateImage({ url: frame.raster_url, coordinates: frame.preview_bounds });
      if (map.getLayer("flood-raster-fill")) {
        map.setPaintProperty("flood-raster-fill", "raster-opacity", 0.82);
      }
      if (map.getLayer("flood-depth-fill")) {
        map.setPaintProperty("flood-depth-fill", "fill-opacity", 0);
      }
    } else {
      if (map.getLayer("flood-raster-fill")) {
        map.setPaintProperty("flood-raster-fill", "raster-opacity", 0);
      }
      if (map.getLayer("flood-depth-fill")) {
        if (frame.stage === "reservoir_rise") {
          // See the matching comment in _transitionToFloodFrame: the lake
          // layer takes over this stage when it has its own frames.
          if (lakeFrames.length) {
            map.setPaintProperty("flood-depth-fill", "fill-opacity", 0);
          } else {
            map.setPaintProperty("flood-depth-fill", "fill-color", "#0284c7");
            map.setPaintProperty("flood-depth-fill", "fill-opacity", 0.68);
          }
        } else {
          map.setPaintProperty("flood-depth-fill", "fill-color", [
            "step", ["get", "depth_class"],
            depthRamp()[0], 1, depthRamp()[0], 2, depthRamp()[1],
            3, depthRamp()[3], 4, depthRamp()[5],
          ]);
          map.setPaintProperty("flood-depth-fill", "fill-opacity", 0.72);
        }
      }
    }
    if (map.getLayer("flood-depth-next-fill")) {
      map.setPaintProperty("flood-depth-next-fill", "fill-opacity", 0);
    }
  } else {
    _transitionToFloodFrame(frame);
  }

  // Hide or dim static reservoir polygon during dynamic pre-breach rise
  const isPreBreach = frame.stage === "reservoir_rise" || frame.stage === "lake_formation" || frame.t_min < 0;
  if (map.getLayer("dam-pool-fill")) {
    map.setPaintProperty("dam-pool-fill", "fill-opacity", isPreBreach ? 0.10 : 0.65);
  }

  const t = Math.round(frame.t_min);
  const readout = document.getElementById("scrubber-time-val");
  if (readout) {
    if (frame.stage === "reservoir_rise" || frame.stage === "lake_formation" || t < 0) {
      const vol = frame.volume_mcm ? ` (${frame.volume_mcm} MCM)` : "";
      const phase = frame.phase_title || (frame.stage === "reservoir_rise" ? "Reservoir Rising" : "Lake Formation");
      readout.textContent = `T-${Math.abs(t)} min · ${phase}${vol}`;
    } else if (t === 0) {
      readout.textContent = "T 0 min · DAM BREACH INITIATION";
    } else {
      readout.textContent = `T+${t} min · Flood Surge Propagation`;
    }
    // CSS ellipses this to one line; the full text still reaches the user
    // via the native title tooltip.
    readout.title = readout.textContent;
  }
  const slider = document.getElementById("time-slider");
  if (slider) slider.value = idx;

  // The timeline and the answer card are both read at the playhead, so they
  // move with the frame rather than being refreshed on their own schedule.
  if (window.Spine) window.Spine.setTime(frame.t_min);
  if (window.refreshAnswerCard) window.refreshAnswerCard(frame.t_min);

  _updateWavefrontMarker(frame, frame.t_min);
  _updateSurgeWaveMarker(frame.t_min);
  _updateRoadCutMarkers(frame.t_min);
  _updateLakeAtTime(frame.t_min);
  _updateDamOvertopBreachFx(frame.t_min);
  _updateVillageDepthFill(frame.t_min);
  _updateEvacRoutesAtTime(frame.t_min);
  _updateFlowFieldTime(frame.t_min);
  _declutterVillageMarkers();
}

// Jump to the frame nearest a given simulated minute. Used by the timeline
// pins and by the priority rows, which know a time but not a frame index.
window.seekToMinute = function (tMin) {
  if (!snapshotFrames.length) return;
  let best = 0, bestD = Infinity;
  for (let i = 0; i < snapshotFrames.length; i++) {
    const d = Math.abs(snapshotFrames[i].t_min - tMin);
    if (d < bestD) { bestD = d; best = i; }
  }
  onTimeSlider(best);
};

// Playback runs at a constant SIMULATED rate, not a constant frame rate.
//
// Frames are not evenly spaced in sim time: pre-breach is ~5 min apart, the
// densified post-breach window is 3 min, and the tail keeps the solver's
// original 15 min. A fixed ms-per-frame interval therefore makes the flood
// appear to move 5x faster in the tail than in the window you actually want
// to watch. Holding ms-per-SIM-MINUTE constant instead makes the front travel
// at one apparent speed for the whole run.
//
// Clamped at both ends: below the floor the browser cannot keep up with the
// raster swap, and above the ceiling the coarse tail simply drags.
const MS_PER_SIM_MIN = 30;
const PLAY_MIN_MS = 70;
const PLAY_MAX_MS = 300;
const PLAY_STEP_MS = 2000;   // fallback only, for a run with no frame times

function _playDelayMs(fromIdx) {
  const a = snapshotFrames[fromIdx], b = snapshotFrames[fromIdx + 1];
  if (!a || !b) return PLAY_MIN_MS;
  const dt = Math.abs((+b.t_min) - (+a.t_min));
  if (!isFinite(dt) || dt <= 0) return PLAY_MIN_MS;
  return Math.max(PLAY_MIN_MS, Math.min(PLAY_MAX_MS, dt * MS_PER_SIM_MIN));
}

// Back to the start without leaving Play running -- the timeline's only other
// transport control.
window.rewindToStart = function () {
  if (!snapshotFrames.length) return;
  _stopPlay();
  onTimeSlider(0);
};

function _setPlayButton(playing) {
  const btn = document.getElementById("btn-play");
  if (!btn) return;
  const label = btn.querySelector("span");
  const icon  = btn.querySelector("svg path");
  if (label) label.textContent = playing ? "Pause" : "Play";
  if (icon) icon.setAttribute("d", playing ? "M3 2h2.5v8H3zM6.5 2H9v8H6.5z" : "M3 2l7 4-7 4z");
  btn.setAttribute("aria-label", playing ? "Pause the flood animation"
                                         : "Play the flood animation");
}

function _stopPlay() {
  if (playTimer) clearTimeout(playTimer);
  playTimer = null;
  _setPlayButton(false);
  if (window.Spine) window.Spine.setPlaying(false);
}

function togglePlay() {
  if (playTimer) { _stopPlay(); return; }

  // Restart from the top if we are sitting on the last frame, so Play always
  // does something rather than silently ending on the frame it starts from.
  if (currentStep >= snapshotFrames.length - 1) {
    _applySnapshotFrame(0);
    _updateRoadLayerAtTime(snapshotFrames[0] ? snapshotFrames[0].t_min : 0);
  }

  _setPlayButton(true);
  // The playhead glides between frames on a linear transition matched to this
  // interval, so discrete frames read as one continuous sweep.
  if (window.Spine) window.Spine.setPlaying(true);

  const tick = () => {
    const next = currentStep + 1;
    if (next >= snapshotFrames.length) { _stopPlay(); return; }
    _applySnapshotFrame(next);
    _checkIsolationAtFrame(next);
    _updateVillageTickerAtTime(
      snapshotFrames[next] ? snapshotFrames[next].t_min : 0);
    _updateRoadLayerAtTime(snapshotFrames[next] ? snapshotFrames[next].t_min : 0);
    const slider = document.getElementById("time-slider");
    if (slider) slider.value = next;
    playTimer = setTimeout(tick, _playDelayMs(next));
  };
  playTimer = setTimeout(tick, _playDelayMs(currentStep));
}

// D5 FIX: null-coercion guard added. Previously `tMin >= null` coerced to
// `tMin >= 0` which is always true — so exactly one village (whichever came
// first in the features array) would always show the callout, even if it
// never isolates. Now we explicitly skip villages with no isolation time.
// Shows the village that isolated MOST RECENTLY at or before the scrubber's
// time. The previous version used one global "already shown" flag and iterated
// features in array order, so whichever village happened to come first won and
// then suppressed every later one -- scrubbing to T+55 could keep displaying a
// village that isolated at T+10 while ignoring one that isolated at T+50.
// `isolationShown` is now the village currently on screen, not a boolean.
let _sortedIsolatedVillages = null;
let _lastIsolationTMin = null;

function _invalidateIsolationIndex() {
  _sortedIsolatedVillages = null;
  _lastIsolationTMin = null;
  isolationShown = null;
  if (_currentPriorityRow) {
    _currentPriorityRow.classList.remove("is-current");
    _currentPriorityRow = null;
  }
}

// ── HADR village impact ticker ───────────────────────────────────────────
// The "Evacuate first" rows already carry each settlement's SOURCED arrival
// time; nothing was reading it against the playhead, so the panel stayed
// static while the wave crossed the villages it lists. This marks a row the
// moment the playhead passes its arrival and flashes it once on the crossing.
//
// `water_arrival_min` is this run's MODELLED arrival, not an observation --
// saying otherwise would be exactly the provenance defect the gates exist to
// catch. Where a settlement also has a DOCUMENTARY observation
// (`validation_arrivals.json`: district logs, railway washout records,
// casualty registers), the row shows both and states the gap. On annamayya
// that gap is large and known: depths land 3/3 in range while arrivals run
// ~100 min late, the recorded 3.3x-slow front. It is shown, not hidden.
const _villageHitState = new Map();
const _villageObs = new Map();          // lowercased name -> validation result

function _loadVillageObservations(jobId) {
  _villageObs.clear();
  // NOTE the `/arrivals` suffix. `/api/validation/{job}` is a different check
  // (observed-extent agreement) and returns `available: false` for annamayya,
  // which silently left the ticker with no records at all.
  return fetch(`/api/validation/${jobId}/arrivals`)
    .then((r) => (r.ok ? r.json() : null))
    .then((v) => {
      (v && v.results ? v.results : []).forEach((o) => {
        if (o && o.name) _villageObs.set(String(o.name).toLowerCase(), o);
      });
    })
    .catch(() => {});
}

// Village names carry their Telugu script in parentheses -- "Mandapalli
// (మండపల్లి)" -- while the observation
// records use the bare Latin name. Match on the part before the bracket.
function _obsFor(name) {
  if (!name) return null;
  const base = String(name).split("(")[0].trim().toLowerCase();
  return _villageObs.get(base) || null;
}

function _updateVillageTickerAtTime(tMin) {
  if (!_priorityRows || !_priorityRows.length) return;
  const list = document.getElementById("priority-list");
  if (!list) return;
  const rows = list.querySelectorAll(".priority-item");

  rows.forEach((row) => {
    const i = +row.dataset.idx;
    const f = _priorityRows[i];
    if (!f) return;
    const arr = f.properties && f.properties.water_arrival_min;
    if (isMissing(arr)) return;

    const hit = tMin >= +arr;
    const was = _villageHitState.get(i) === true;
    if (hit === was) return;
    _villageHitState.set(i, hit);
    row.classList.toggle("is-hit", hit);

    let badge = row.querySelector(".p-hit");
    if (hit) {
      if (!badge) {
        badge = document.createElement("span");
        badge.className = "p-hit";
        const nameEl = row.querySelector(".p-name");
        if (nameEl) nameEl.appendChild(badge);
      }
      badge.textContent = `WATER T+${Math.round(+arr)}m`;
      const obs = _obsFor(f.properties.village_name);
      let obsEl = row.querySelector(".p-obs");
      if (obs && Array.isArray(obs.obs_arrival_min_range)) {
        if (!obsEl) {
          obsEl = document.createElement("span");
          obsEl.className = "p-obs";
          const nameEl2 = row.querySelector(".p-name");
          if (nameEl2) nameEl2.appendChild(obsEl);
        }
        const [lo, hi] = obs.obs_arrival_min_range;
        const err = obs.arrival_signed_error_min;
        obsEl.textContent =
          `recorded T${lo >= 0 ? "+" : ""}${Math.round(lo)}…${hi >= 0 ? "+" : ""}${Math.round(hi)}m`
          + (err == null ? "" : ` · ${obs.arrival_status} ${err > 0 ? "+" : ""}${Math.round(err)}m`);
        obsEl.title = obs.source || "";
      } else if (obsEl) {
        obsEl.remove();
      }
      // Restart the flash even if the class is already on the node.
      row.classList.remove("just-hit");
      void row.offsetWidth;
      row.classList.add("just-hit");
    } else if (badge) {
      badge.remove();
      const stale = row.querySelector(".p-obs");
      if (stale) stale.remove();
      row.classList.remove("just-hit");
    }
  });
}

function _checkIsolationAtFrame(idx) {
  if (!simulationResults || !simulationResults.features) return;
  const tMin = snapshotFrames[idx] ? snapshotFrames[idx].t_min : 0;
  const roundT = Math.round(tMin);
  if (roundT === _lastIsolationTMin) return;
  _lastIsolationTMin = roundT;

  if (!_sortedIsolatedVillages) {
    _sortedIsolatedVillages = (simulationResults.features || [])
      .filter(f => !isMissing(f.properties?.isolation_time_min))
      .map(f => ({ t: +f.properties.isolation_time_min, feature: f }))
      .sort((a, b) => a.t - b.t);
  }

  let latest = null;
  for (let i = _sortedIsolatedVillages.length - 1; i >= 0; i--) {
    if (_sortedIsolatedVillages[i].t <= tMin) {
      latest = _sortedIsolatedVillages[i].feature;
      break;
    }
  }

  // The popover no longer opens itself on every scrub. A panel that appears
  // unbidden while you are dragging is noise; the answer card already carries
  // the running count, and the timeline already marks the event. What this
  // still does is mark WHICH village most recently lost its road, so the
  // priority list can show it.
  if (!latest) {
    if (isolationShown !== null) {
      isolationShown = null;
      _markCurrentVillage(null);
    }
    return;
  }
  const id = latest.properties.village_id || latest.properties.village_name;
  if (id !== isolationShown) {
    isolationShown = id;
    _markCurrentVillage(latest.properties.village_name);
  }
}

let _priorityRowDomByName = new Map();
let _currentPriorityRow = null;

function _markCurrentVillage(name) {
  if (_currentPriorityRow) {
    _currentPriorityRow.classList.remove("is-current");
    _currentPriorityRow = null;
  }
  if (name) {
    const row = _priorityRowDomByName.get(name.trim());
    if (row) {
      row.classList.add("is-current");
      _currentPriorityRow = row;
    } else {
      document.querySelectorAll(".priority-item").forEach((r) => {
        const el = r.querySelector(".p-name");
        if (el && el.textContent.trim() === name.trim()) {
          r.classList.add("is-current");
          _currentPriorityRow = r;
        }
      });
    }
  }
}


// ── Demo results ──────────────────────────────────────────────────────────
// Paints the empty state once, at startup. It must never fire over a finished
// run: MapLibre does not load its style while the canvas is hidden, so a run
// started and completed in a background tab is followed by the map's "load"
// event firing on return -- which used to wipe a real result back to zeros.
async function _loadSimulationRun(job_id, dam_name) {
  // New per-run layers reset before the fetches below repopulate them, so a
  // run with none of this data (every scenario but annamayya_compound today)
  // leaves the map exactly as before -- no stale layer from a previous run.
  lakeFrames = [];
  _lastLakeT = null;
  _breachFlashPlayed = false;
  frontField = null;
  evacRoutesGeoJSON = null;
  villageDepthSeries = null;
  selectedEvacVillageId = null;
  _lastEvacT = null;
  _arrivalRingPlayed.clear();
  flowLastFrameT = 0;
  _stopFlowLoop();
  if (map.getSource("water-planes")) {
    map.getSource("water-planes").setData({ type: "FeatureCollection", features: [] });
  }
  if (map.getSource("lake")) {
    map.getSource("lake").setData({ type: "FeatureCollection", features: [] });
  }
  if (map.getSource("evac-routes")) {
    map.getSource("evac-routes").setData({ type: "FeatureCollection", features: [] });
  }
  _clearEvacRouteMarkers();

  try {
    if (window.FloodSightDebug) {
      window.FloodSightDebug.log.push({
        t: +(performance.now() / 1000).toFixed(3),
        level: "info",
        tag: "backend",
        msg: `Loading simulation run ${job_id}`,
        extra: { job_id },
      });
    }

    const res = await fetchJson(`/api/results/${job_id}`, "Results request");
    _invalidateIsolationIndex();
    simulationResults = res;
    _renderVillages(res);
    renderPriorityList(res.features || []);
    updateCounters(res.features || []);
    if (window.Spine) window.Spine.setEvents(res.features || []);
    if (window.setAnswerCardReady) window.setAnswerCardReady();

    let hyd = null;
    try {
      hyd = await fetchJson(`/api/hydrograph/${job_id}`, "Hydrograph request");
      renderHydrograph(hyd);
      updateBreachCards(hyd);
      if (window.Spine) window.Spine.setFlow(hyd);
      _populateAlertPanels(res.features || [], dam_name || "Dam Breach", hyd);
    } catch (e) {
      console.warn("Hydrograph load:", e);
    }

    ["shp", "kml", "cap"].forEach(fmt => {
      const b = document.getElementById(`btn-${fmt}`);
      if (b) b.disabled = false;
    });

    await _loadSnapshotFrames(job_id);

    try {
      const roadsResp = await fetch(`/api/roads/${job_id}`);
      if (roadsResp.ok) {
        roadsGeoJSON = await roadsResp.json();
        _clearRoadCutPool();
        const roadSrc = map.getSource("roads-timeline");
        if (roadSrc) roadSrc.setData(roadsGeoJSON);
        if (window.Spine) window.Spine.setRoads(roadsGeoJSON);
        if (window.refreshAnswerCard) {
          window.refreshAnswerCard(snapshotFrames.length ? snapshotFrames[currentStep].t_min : 0);
        }
      }
    } catch (e) {
      console.warn("Road timeline not available:", e);
    }

    try {
      const envResp = await fetch(`/api/envelope_geojson/${job_id}`);
      if (envResp.ok) {
        const envGeoJSON = await envResp.json();
        const envSrc = map.getSource("envelope");
        if (envSrc) envSrc.setData(envGeoJSON);
      }
    } catch (e) {
      console.warn("Envelope not available:", e);
    }

    // Each of these is its own scenario's optional extra data; a 404 on any
    // one leaves that layer empty rather than breaking the others.
    await _loadLakeFrames(job_id);
    if (snapshotFrames.length) {
      _updateLakeAtTime(snapshotFrames[currentStep] ? snapshotFrames[currentStep].t_min : 0);
    }
    await _loadWaterPlanes(job_id);
    await _loadFrontField(job_id);
    await _loadEvacRoutes(job_id);
    await _loadVillageDepth(job_id);
    _updateStoryButton();

    await _loadValidation(job_id);
  } catch (err) {
    console.warn(`Failed to load simulation run ${job_id}:`, err);
  }

  // Outside the try above, and in its own. The verdict badge must not be
  // skipped because an earlier step threw -- a run whose validation layer
  // failed to load is exactly one whose validity the viewer should still see.
  try {
    await _renderRunValidityBadge(job_id);
  } catch (e) {
    console.warn("validity badge not rendered:", e);
  }
}

// ── Does the run on screen pass its own gates? ────────────────────────────
// Until 2026-09-19 the frontend contained the string "validity" exactly zero
// times: the map drew a flood extent and never said whether the run behind it
// had passed G1-G5. That matters more here than in most tools, because the one
// phutkal run the API will serve is a LEGACY certification -- it carries
// `validity.valid: true` from before the gates could fail, and
// `tests/test_mass_gates.py` uses that same run as its example of a false
// certification. Drawing its inundation with no verdict attached is the exact
// thing the rest of this project refuses to do.
//
// Reuses the .dam-breach-mode-badge / .badge-label / .badge-val classes the
// "Geometry Not Validated" callout already uses, so no new CSS.
async function _renderRunValidityBadge(job_id) {
  const existing = document.getElementById("run-validity-badge");
  if (existing) existing.remove();
}


async function _autoLoadScenarioSimulation(scenarioKey) {
  try {
    const resp = await fetch(`/api/scenarios/${scenarioKey}/latest_job`);
    if (!resp.ok) {
      console.info(`No pre-computed job found for scenario: ${scenarioKey}`);
      return;
    }
    const data = await resp.json();
    if (!data || !data.job_id) return;
    await _loadSimulationRun(data.job_id, data.dam_name);
  } catch (err) {
    console.warn(`Could not autoload scenario simulation for ${scenarioKey}:`, err);
  }
}

function _loadDemoResults() {
  if (simulationResults && (simulationResults.features || []).length) return;
  const initialKey = document.getElementById("scenario-select")?.value || "rishiganga";
  _autoLoadScenarioSimulation(initialKey);
}

function _renderVillages(geojson) {
  // Settlements this run never wetted are dropped from the POLYGON layer as well
  // as the markers. Removing only the marker left an unlabelled circle sitting on
  // the map with nothing to explain it — worse than the badge, because a ring
  // drawn around a village reads as "something happened here".
  //
  // Both layers filter on the same `villageStatus` call, so they cannot disagree.
  // `_updateVillageMarkers` keeps its own check: it is also called from
  // `_loadContextLayers` with the raw village layer, which carries no depth at
  // all and must still draw every settlement before a run exists.
  const shown = Object.assign({}, geojson, {
    features: (geojson.features || []).filter(
      (f) => villageStatus(f.properties || {}).statusClass !== "is-dry"),
  });
  if (map.getSource("villages")) {
    map.getSource("villages").setData(shown);
  }
  _updateVillageMarkers(shown);
}

// ── Time scrubber ──────────────────────────────────────────────────────────
// D11 FIX: Deleted the demo fallback that hardcoded maxMin=360 and a magic
// isolation threshold of tMin >= 47. The scrubber only operates on real data.
// If no real frames are loaded, the scrubber is inactive (no flood layer to drive).
let _sliderRaf = null;
let _pendingSliderVal = null;

function onTimeSlider(value) {
  _pendingSliderVal = parseInt(value, 10);
  if (Number.isNaN(_pendingSliderVal)) return;
  if (_sliderRaf) return;
  _sliderRaf = requestAnimationFrame(() => {
    _sliderRaf = null;
    const idx = _pendingSliderVal;
    if (snapshotFrames.length > 0 && idx !== currentStep) {
      _applySnapshotFrame(idx);
      _checkIsolationAtFrame(idx);
      _updateVillageTickerAtTime(
        snapshotFrames[idx] ? snapshotFrames[idx].t_min : 0);
      const tMin = snapshotFrames[idx] ? snapshotFrames[idx].t_min : 0;
      _updateRoadLayerAtTime(tMin);
    }
  });
}

// ── Village detail ────────────────────────────────────────────────────────
// Opens the timeline popover on a village and moves the playhead to the moment
// it loses its last road, so the map behind the popover shows that moment.
// The window sentence itself is written by ui.js from the two timestamps
// rather than from the signed evacuation_window_min, whose sign is flagged as
// unverified in ui.md.
function showIsolationCallout(p) {
  if (!p || !window.showEventPop) return;
  const t = isMissing(p.isolation_time_min)
    ? (isMissing(p.water_arrival_min) ? 0 : +p.water_arrival_min)
    : +p.isolation_time_min;
  if (window.seekToMinute) window.seekToMinute(t);
  window.showEventPop({ t, label: p.village_name || "Village", payload: p }, null);
}

// ── Tab switching ─────────────────────────────────────────────────────────
function switchTab(tab) {
  document.querySelectorAll(".tab").forEach(btn => btn.classList.remove("is-active"));
  const tabBtn = document.getElementById(`tab-${tab}`);
  if (tabBtn) tabBtn.classList.add("is-active");

  // Hide all fullpage panels
  ["compare", "about", "alerts"].forEach(t => {
    const panel = document.getElementById(`tab-${t}-panel`);
    if (panel) panel.classList.add("hidden");
  });

  if (tab === "compare" || tab === "about" || tab === "alerts") {
    const panel = document.getElementById(`tab-${tab}-panel`);
    if (panel) panel.classList.remove("hidden");
    if (tab === "compare") {
      renderRitterPlot();
      if (window.renderMalpassetBenchmark) window.renderMalpassetBenchmark();
      if (window.renderScenarioSolverComparison) window.renderScenarioSolverComparison();
    }
  }
}

// ── Alert panel population (Phase 3) ──────────────────────────────────────
// Fills CAP JSON preview, responder brief, and SMS previews from simulation results.
// Severity is derived from the evacuation window, not asserted. The previous
// version hardcoded severity "Extreme" / certainty "Likely" on every alert
// regardless of what was computed, which states a conclusion the model did not
// reach -- on the highest-stakes surface in the product.
function _alertSeverity(p) {
  const win = p.evacuation_window_min;
  const arr = p.water_arrival_min;
  const pop = isMissing(p.pop_at_risk) ? 0 : +p.pop_at_risk;
  if (isMissing(win) && isMissing(arr)) {
    return { severity: "Unknown", certainty: "Unknown",
             urgency: "Unknown", why: "neither an evacuation window nor an arrival time was computed" };
  }
  if (!isMissing(win) && +win < 15 && pop > 0) {
    return { severity: "Extreme", certainty: "Likely", urgency: "Immediate",
             why: `evacuation window ${Math.round(+win)} min with ${pop} people` };
  }
  if ((!isMissing(win) && +win < 60) || (!isMissing(arr) && +arr < 30)) {
    return { severity: "Severe", certainty: "Likely", urgency: "Expected",
             why: "evacuation window under an hour or water arriving within 30 min" };
  }
  return { severity: "Moderate", certainty: "Possible", urgency: "Future",
           why: "inundated with over an hour of evacuation window" };
}

function _populateAlertPanels(rankedFeatures, damName, hyd) {
  if (!rankedFeatures || rankedFeatures.length === 0) return;
  const now = new Date().toISOString();
  const top = rankedFeatures[0].properties;
  const sev = _alertSeverity(top);
  // "not computed" -- never 0. An alert that says "PAR: 0" because the number
  // was missing tells a responder there is nobody to evacuate.
  const par   = isMissing(top.pop_at_risk)      ? "not computed" : (+top.pop_at_risk).toLocaleString();
  const isoT  = isMissing(top.isolation_time_min) ? "not computed" : `T+${Math.round(+top.isolation_time_min)} min`;
  const winT  = isMissing(top.evacuation_window_min) ? "not computed" : `${Math.round(+top.evacuation_window_min)} min`;
  const hosp  = isMissing(top.hospitals_flooded) ? "not computed" : top.hospitals_flooded;
  const schl  = isMissing(top.schools_flooded)   ? "not computed" : top.schools_flooded;
  const vname = top.village_name || "unknown";
  const Qp = hyd && hyd.central && hyd.central.Q_p
    ? Math.round(hyd.central.Q_p) : "—";

  // CAP 1.2 preview
  const capEl = document.getElementById("cap-preview");
  if (capEl) capEl.textContent = JSON.stringify({
    alert: {
      identifier: `FLOODSIGHT-${now}`,
      sender: "FloodSense / SIH26161",
      sent: now,
      status: "Exercise",
      msgType: "Alert",
      scope: "Restricted",
      note: "NOT TRANSMITTED — FloodSense output for responder planning only",
      info: {
        language: "en-IN",
        category: "Flood",
        event: `Dam-break: ${damName}`,
        urgency: sev.urgency,
        severity: sev.severity,
        certainty: sev.certainty,
        severityBasis: sev.why,
        description: `Peak discharge ~${Qp} m3/s. Priority village: ${vname} (PAR: ${par}). Isolation ${isoT}. Evac window: ${winT}.`,
        contact: "NDMA",
      },
    },
  }, null, 2);

  // Responder brief
  const briefEl = document.getElementById("responder-brief");
  if (briefEl) briefEl.textContent = [
    `INCIDENT: ${damName}`,
    `TIME:     ${now}`,
    `PEAK DISCHARGE: ~${Qp} m3/s (Froehlich central)`,
    ``,
    `SEVERITY: ${sev.severity} (${sev.why})`,
    ``,
    `TOP PRIORITY:`,
    `  Village:      ${vname}`,
    `  PAR:          ${par} people`,
    `  Isolation:    ${isoT}`,
    `  Evac window:  ${winT}`,
    `  Hospitals:    ${hosp}   Schools: ${schl}`,
    ``,
    `${rankedFeatures.length} villages ranked. Export via /api/export/{job_id}/shp`,
  ].join("\n");

  // SMS English (160-char max)
  const smsEn = document.getElementById("sms-en");
  if (smsEn) smsEn.textContent =
    `FLOOD ALERT: Dam break ${(damName||"").substring(0,20)}. EVACUATE ${vname.substring(0,12)}. Move to high ground immediately. -NDMA`.substring(0, 160);

  // SMS Hindi (70 Unicode chars per segment)
  const smsHi = document.getElementById("sms-hi");
  if (smsHi) smsHi.textContent =
    `बाढ़ चेतावनी! ${vname.substring(0,8)}: तुरंत ऊंचे स्थान पर जाएं। -NDMA`.substring(0, 70);
}

// ── API: Run simulation ───────────────────────────────────────────────────
async function runSimulation() {
  const btn = document.getElementById("btn-run");
  const statusEl = document.getElementById("run-status");

  btn.disabled = true;
  btn.textContent = "Running…";
  statusEl.className = "status-bar status-running";
  statusEl.textContent = "Starting the breach estimate…";

  const body = {
    scenario_key:             document.getElementById("scenario-select").value,
    dam_name:                 document.getElementById("dam-name-input").value,
    failure_mode:             document.getElementById("failure-mode").value,
    reservoir_level_fraction: parseFloat(document.getElementById("reservoir-fraction").value),
    // Grid coarsening. The solver cost scales roughly with the cell count and
    // again with the smaller timestep a finer grid forces, so this is by far
    // the biggest control the analyst has over run time. The chosen cell size
    // is displayed in the header, so the trade is visible rather than hidden.
    coarsen: parseInt(document.getElementById("resolution-select").value, 10),
  };

  try {
    if (window.FloodSightDebug) {
      window.FloodSightDebug.log.push({
        t: +(performance.now() / 1000).toFixed(3),
        level: "info",
        tag: "api",
        msg: `POST /api/run request sent for ${body.scenario_key}`,
        extra: { payload: body },
      });
    }

    const runResp = await fetch("/api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const runJson = await runResp.json();
    if (!runResp.ok) {
      throw new Error(`run API failed (${runResp.status}): ${runJson?.detail || JSON.stringify(runJson)}`);
    }
    const { job_id } = runJson;
    currentJobId = job_id;
    window.currentJobId = job_id;   // expose for charts.js

    if (window.FloodSightDebug) {
      window.FloodSightDebug.log.push({
        t: +(performance.now() / 1000).toFixed(3),
        level: "info",
        tag: "api",
        msg: `Job ${job_id} accepted by backend`,
        extra: { job_id, status: "queued", payload: body },
      });
    }

    // Multi-stage progress labels
    const STAGE_LABELS = {
      queued:                  "Queued…",
      computing_hydrodynamics: "Routing the water…",
      computing_exposure:      "Finding who is hit…",
      done:                    "Done.",
      error:                   "Something went wrong.",
    };

    // A wall-clock cap on polling is NOT a failure signal. The previous code
    // gave up after `attempts > 90` (90 x 1500 ms = 135 s) and printed
    // "Simulation failed" -- while the solver was still running perfectly well.
    // A Rishiganga run at coarsen 2 takes 500 s+, so the UI reported failure on
    // every honest run. Only the server may declare failure; the client just
    // reports how long it has been waiting.
    let attempts = 0;
    let netErrors = 0;
    let settled = false;   // set true once this run reaches done/error — guards
                            // against the visibilitychange catch-up below and the
                            // interval firing again in the same tick
    const startedAt = Date.now();

    const tick = async () => {
      if (settled) return;
      attempts++;
      let st;
      try {
        const r = await fetch(`/api/status/${job_id}`);
        if (!r.ok) throw new Error(`status ${r.status}`);
        st = await r.json();
        netErrors = 0;
        if (window.FloodSightDebug) {
          window.FloodSightDebug.log.push({
            t: +(performance.now() / 1000).toFixed(3),
            level: (st.status === "error" ? "error" : "info"),
            tag: "backend",
            msg: `status ${st.status} for job ${job_id}`,
            extra: { job_id, status: st.status, progress: st.progress, metrics: st.metrics, cell_size_m: st.cell_size_m },
          });
        }
      } catch (e) {
        // A blip is not a failed simulation. Only give up if the server has
        // been unreachable for several consecutive polls.
        if (++netErrors < 10) return;
        settled = true;
        clearInterval(pollHandle);
        window.removeEventListener('visibilitychange', onVisible);
        statusEl.className = "status-bar status-error";
        statusEl.textContent = `Lost contact with the server — ${e.message}`;
        if (window.FloodSightDebug) {
          window.FloodSightDebug.log.push({
            t: +(performance.now() / 1000).toFixed(3),
            level: "error",
            tag: "backend",
            msg: `status polling failed for job ${job_id}: ${e.message}`,
            extra: { job_id, error: String(e) },
          });
        }
        btn.disabled = false;
        btn.textContent = "Run it";
        return;
      }
      const mins = Math.floor((Date.now() - startedAt) / 60000);
      const secs = Math.floor((Date.now() - startedAt) / 1000) % 60;
      const elapsed = mins ? ` — ${mins}m ${secs}s elapsed` : ` — ${secs}s elapsed`;
      statusEl.textContent =
        (STAGE_LABELS[st.status] || `${st.status}…`) +
        (st.status === "done" ? "" : elapsed);
      _updateProgressSteps(st.status);
      _renderRunProgress(st.progress, st.status);

      if (st.status === "done") {
        settled = true;
        clearInterval(pollHandle);
        window.removeEventListener('visibilitychange', onVisible);
        statusEl.className = "status-bar status-done";
        btn.disabled = false;
        btn.textContent = "Run it";

        // The cell size is the one number about the run itself worth keeping on
        // screen — it is the speed/detail trade the analyst chose. Everything
        // on this page is computed, so nothing is badged as such.
        const cellSz = st.cell_size_m;
        if (cellSz) {
          const hdCell = document.getElementById("header-cell-size");
          if (hdCell) { hdCell.textContent = `${cellSz} m`; hdCell.hidden = false; }
          const solverCell = document.getElementById("solver-cellsize");
          if (solverCell) solverCell.textContent = `${cellSz} m`;
        }

        // Load village results → update map + UI
        if (window.FloodSightDebug) {
          window.FloodSightDebug.log.push({
            t: +(performance.now() / 1000).toFixed(3),
            level: "info",
            tag: "backend",
            msg: `job ${job_id} finished; fetching result payloads`,
            extra: { job_id, status: "done" },
          });
        }

        await _loadSimulationRun(job_id, body.dam_name);

      } else if (st.status === "error") {
        settled = true;
        clearInterval(pollHandle);
        window.removeEventListener('visibilitychange', onVisible);
        statusEl.className = "status-bar status-error";
        statusEl.textContent = st.error || "The simulation did not finish.";
        if (window.FloodSightDebug) {
          window.FloodSightDebug.log.push({
            t: +(performance.now() / 1000).toFixed(3),
            level: "error",
            tag: "backend",
            msg: `job ${job_id} reported error: ${st.error || "Simulation failed"}`,
            extra: { job_id, status: st.status, error: st.error, progress: st.progress },
          });
        }
        btn.disabled = false;
        btn.textContent = "Run it";
      }
    };

    // Chrome throttles (and can fully suspend) setInterval callbacks in a
    // backgrounded tab -- a real risk here since a run can take several
    // minutes and tabbing away to do something else while it runs is the
    // normal way to use this page. Without this, coming back to the tab could
    // show a stale "Running…" status and stale map/village data for a run
    // that actually finished minutes ago, because the throttled interval
    // hadn't gotten around to firing the completion handler yet.
    // visibilitychange fires immediately on refocus regardless of interval
    // throttling, so it forces an immediate catch-up check.
    const onVisible = () => { if (document.visibilityState === "visible") tick(); };
    window.addEventListener("visibilitychange", onVisible);

    const pollHandle = setInterval(tick, 1500);
    tick();   // first check immediately rather than waiting out the first 1.5 s

  } catch (err) {
    statusEl.className = "status-bar status-error";
    statusEl.textContent = "Could not start the run — " + err.message;
    if (window.FloodSightDebug) {
      window.FloodSightDebug.log.push({
        t: +(performance.now() / 1000).toFixed(3),
        level: "error",
        tag: "api",
        msg: `run API setup failed: ${err.message}`,
        extra: { error: String(err), stack: err.stack },
      });
    }
    btn.disabled = false;
    btn.textContent = "Run it";
  }
}

// Determinate progress bar. `progress` comes from the pipeline itself: stage
// weights measured from a profiled run, plus the solver's own
// simulated-time fraction, reported on a ~1 s throttle from inside its loop.
// An indeterminate spinner cannot distinguish "working" from "wedged", which
// is why a 12-minute solve read as "it never seems to end".
function _renderRunProgress(prog, status) {
  const box = document.getElementById("run-progress");
  if (!box) return;
  if (!prog) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");

  const pct = Math.round(Math.min(1, Math.max(0, +prog.frac || 0)) * 100);
  const fill = document.getElementById("rp-fill");
  if (fill) fill.style.width = pct + "%";
  box.setAttribute("aria-valuenow", String(pct));

  const label = document.getElementById("rp-label");
  if (label) label.textContent = prog.label || "";

  // ETA is an extrapolation from the fraction done so far, so it is labelled
  // "~" and withheld entirely until there is enough of the run to base it on.
  const pctEl = document.getElementById("rp-pct");
  if (pctEl) {
    let txt = `${pct}%`;
    if (status !== "done" && prog.eta_s != null && prog.eta_s > 0) {
      const e = Math.round(prog.eta_s);
      txt += e >= 60 ? ` · ~${Math.floor(e / 60)}m ${e % 60}s left`
                     : ` · ~${e}s left`;
    }
    pctEl.textContent = txt;
  }

  const det = document.getElementById("rp-detail");
  if (det) det.textContent = prog.detail || "";

  // Log progress to debug panel for visibility
  if (window.FloodSightDebug && prog.label) {
    window.FloodSightDebug.log.push({
      t: +(performance.now() / 1000).toFixed(3),
      level: "info",
      tag: "progress",
      msg: `${prog.label} (${pct}%)`,
      extra: { stage: prog.label, frac: pct, eta_s: prog.eta_s, elapsed_s: prog.elapsed_s },
    });
  }
}

// Progress step tracker helper (4-stage pipeline)
function _updateProgressSteps(status) {
  // Map API status strings to which steps are done/active
  const STATUS_MAP = {
    queued:                    0,   // step 0 active
    computing_hydrodynamics:   1,   // step 1 active
    computing_exposure:        2,   // step 2 active
    done:                      3,   // all done
  };
  const activeIdx = STATUS_MAP[status] ?? -1;
  const steps = document.querySelectorAll(".progress-step");
  steps.forEach((s, i) => {
    s.classList.toggle("step-done",    i < activeIdx);
    s.classList.toggle("step-active",  i === activeIdx);
    s.classList.toggle("step-pending", i > activeIdx);
  });
  // All done: mark all steps as done
  if (status === "done") {
    steps.forEach(s => {
      s.classList.remove("step-active", "step-pending");
      s.classList.add("step-done");
    });
  }
}

// ── Export ────────────────────────────────────────────────────────────────
async function downloadExport(fmt) {
  if (!currentJobId) return;
  window.location.href = `/api/export/${currentJobId}/${fmt}`;
}
window.downloadExport = downloadExport;

// ── Counters ──────────────────────────────────────────────────────────────
// D2 FIX: removed `Math.round(totalBldg * 1.3)` fabricated road-edge count.
//   The val-roads counter now shows the real cut count from roads_timeline.
//   Until M6 road data loads (after a run), the element shows "—".
// D12 FIX: "Villages Isolated" was counting villages with short evacuation
//   windows (> 0 && < 60 min), not actually-isolated villages. Now counts
//   villages where isolation_time_min is not null (i.e. they lost road access).
function updateCounters(features) {
  // Summing with `|| 0` treats "not computed" as "zero people", which silently
  // understates the total and then presents it as a complete figure. The sum
  // still skips missing values -- there is nothing else to add -- but the count
  // of what was skipped is surfaced on the tile instead of being swallowed.
  const sumKnown = (key) => features.reduce(
    (acc, f) => isMissing(f.properties[key]) ? acc : acc + (+f.properties[key]), 0);
  const countMissing = (key) =>
    features.filter(f => isMissing(f.properties[key])).length;

  const totalPAR  = sumKnown("pop_at_risk");
  const totalBldg = sumKnown("buildings_flooded");
  for (const [id, key, label] of [["val-par", "pop_at_risk", "population"],
                                  ["val-buildings", "buildings_flooded", "building count"]]) {
    const el = document.getElementById(id);
    if (!el) continue;
    const n = countMissing(key);
    el.title = n
      ? `Partial total: ${n} of ${features.length} villages have no computed ${label}.`
      : "";
    el.classList.toggle("partial-total", n > 0);
  }

  // These two are whole-event totals, so they are counted up once when the run
  // lands. Roads cut and villages cut off are NOT set here -- they are read at
  // the playhead by refreshAnswerCard(), from the per-link cut times and the
  // per-village isolation times, so they change as you scrub.
  //
  // When EVERY value is missing there is no total to show, and rendering the
  // empty sum as "0" states a finding the run never made -- "0 buildings
  // flooded" reads as "we checked and none were", not "we never counted".
  // A routed inundation over an AOI with no OSM building layer is exactly that
  // case. Show the em dash the markup ships with instead.
  const showTotal = (id, key, total) => {
    const el = document.getElementById(id);
    if (el && countMissing(key) === features.length && features.length > 0) {
      _counterRuns.get(id)?.finish?.();
      el.textContent = "—";
      el.title = `Not computed: no ${key.replace(/_/g, " ")} is available for this run.`;
      return;
    }
    animateCounter(id, total, ",");
  };
  showTotal("val-par",       "pop_at_risk",       totalPAR);
  showTotal("val-buildings", "buildings_flooded", totalBldg);
}

// Counts up to the figure once, on a frame clock rather than a 30 ms timer, so
// it stays smooth on a busy main thread and stops on the exact target.
// Guarded on both sides: a missing element used to throw inside the interval
// forever, and a NaN target made the stop condition unreachable.
const _counterRuns = new Map();   // id -> { raf, finish }

// Frames stop the moment the tab goes to the background, so any count still in
// flight is settled on its real value here instead of being left frozen on a
// partial number that reads like a result.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) return;
  for (const run of _counterRuns.values()) {
    cancelAnimationFrame(run.raf);
    run.finish();
  }
  _counterRuns.clear();
});

function animateCounter(id, target, sep) {
  const el = document.getElementById(id);
  if (!el) return;
  if (!Number.isFinite(+target)) { el.textContent = "—"; return; }
  target = +target;

  const prev = _counterRuns.get(id);
  if (prev) cancelAnimationFrame(prev.raf);

  const write = (v) => {
    el.textContent = sep === "," ? Math.round(v).toLocaleString() : String(Math.round(v));
  };
  // A hidden tab never gets an animation frame, so a run that completes in the
  // background would leave this showing a dash forever. Same for reduced
  // motion, where the count-up is not wanted at all.
  if (document.hidden || target === 0 ||
      window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    write(target);
    return;
  }

  const DUR = 760;
  const t0 = performance.now();
  const tick = (now) => {
    // Tab hidden mid-count: no further frames are coming, so land on the real
    // figure now rather than freezing on whatever partial number was last
    // written. A stalled count-up reads as a result, not as an animation.
    if (document.hidden) { write(target); _counterRuns.delete(id); return; }
    const p = Math.min(1, (now - t0) / DUR);
    const eased = 1 - Math.pow(1 - p, 3);        // ease-out cubic: fast, then settles
    write(target * eased);
    if (p < 1) _counterRuns.set(id, { raf: requestAnimationFrame(tick), finish });
    else { finish(); _counterRuns.delete(id); }
  };
  const finish = () => write(target);
  _counterRuns.set(id, { raf: requestAnimationFrame(tick), finish });
}

// ── Priority list ─────────────────────────────────────────────────────────
// Holds the rows the list is currently showing, so a click can look its row up
// by index instead of round-tripping the whole properties object through an
// HTML attribute.
let _priorityRows = [];

function renderPriorityList(features) {
  const list = document.getElementById("priority-list");
  if (!list) return;

  // An empty result must clear the list. Returning early left the PREVIOUS
  // run's ranking on screen, attributed to the run that just produced nothing.
  if (!features || features.length === 0) {
    _priorityRows = [];
    _priorityRowDomByName.clear();
    _currentPriorityRow = null;
    list.innerHTML =
      '<div class="priority-empty">Run a simulation to see the village ranking</div>';
    return;
  }

  _priorityRows = [...features].sort(
    (a, b) => a.properties.priority_rank - b.properties.priority_rank);

  list.innerHTML = _priorityRows.map((f, i) => {
    const p = f.properties;
    const rankClass = p.priority_rank <= 3 ? `rank-${p.priority_rank}` : "";
    const isolatedClass =
      p.evacuation_window_min > 0 && p.evacuation_window_min < 60 ? "isolated" : "";
    const scoreW = isMissing(p.priority_score)
      ? 0 : Math.round(Math.max(0, Math.min(1, +p.priority_score)) * 100);

    // village_name is OSM-sourced third-party text going into innerHTML, and
    // the row used to serialise the whole properties object into an inline
    // onclick attribute -- a name containing a quote broke the markup and let
    // arbitrary text into an attribute. Escaped, and the handler is delegated.
    return `
      <div class="priority-item ${isolatedClass}" role="listitem" data-idx="${i}">
        <div class="p-rank ${rankClass}">${escapeHtml(p.priority_rank)}</div>
        <div>
          <div class="p-name" dir="auto">${escapeHtml(p.village_name)}</div>
          <div class="p-detail">
            ${fmtNum(p.pop_at_risk)} people · water ${fmtMin(p.water_arrival_min)} · exit gone ${fmtMin(p.isolation_time_min)}
          </div>
        </div>
        <div class="p-score-col">
          <span class="p-score">${fmtFixed(p.priority_score)}</span>
          <div class="p-score-bar"><div class="p-score-fill" style="width:${scoreW}%"></div></div>
          <span class="p-window">win: ${fmtWin(p.evacuation_window_min)}</span>
        </div>
      </div>
    `;
  }).join("");

  _priorityRowDomByName.clear();
  _currentPriorityRow = null;
  list.querySelectorAll(".priority-item").forEach((row) => {
    const el = row.querySelector(".p-name");
    if (el && el.textContent) {
      _priorityRowDomByName.set(el.textContent.trim(), row);
    }
  });

  if (!list._fsDelegated) {
    list.addEventListener("click", (ev) => {
      const row = ev.target.closest(".priority-item[data-idx]");
      if (!row) return;
      const f = _priorityRows[+row.dataset.idx];
      if (f) {
        showIsolationCallout(f.properties);
        if (window.showEvacRouteFor) window.showEvacRouteFor(f.properties.village_id);
      }
    });
    // Hovering a row previews that village's route without moving the
    // playhead or opening the popover -- a lighter-weight interaction than
    // click, matched by leaving on mouseleave.
    list.addEventListener("mouseover", (ev) => {
      const row = ev.target.closest(".priority-item[data-idx]");
      if (!row) return;
      const f = _priorityRows[+row.dataset.idx];
      if (f && window.showEvacRouteFor) window.showEvacRouteFor(f.properties.village_id);
    });
    list.addEventListener("mouseleave", () => {
      if (window.showEvacRouteFor) window.showEvacRouteFor(null);
    });
    list._fsDelegated = true;
  }
}

// ── Breach cards ──────────────────────────────────────────────────────────
function updateBreachCards(hyd) {
  if (!hyd || !hyd.central) return;
  const arms = ["pessimistic", "central", "optimistic"];
  const prefix = ["bp", "bc", "bo"];
  arms.forEach((arm, i) => {
    const d = hyd[arm];
    if (!d) return;
    const Qp = d.Q_p ? (+d.Q_p).toLocaleString(undefined, {maximumFractionDigits:0}) : "—";
    const tf = d.t_f ? (+d.t_f).toLocaleString(undefined, {maximumFractionDigits:1}) : "—";
    const qEl = document.getElementById(`${prefix[i]}-qp`);
    const tEl = document.getElementById(`${prefix[i]}-tf`);
    if (qEl) { qEl.textContent = `${Qp} m³/s`; qEl.title = "Peak outflow"; }
    if (tEl) { tEl.textContent = `${tf} s`;    tEl.title = "Time to form the breach"; }
  });
}

// ── M10: validation against the observed outcome ──────────────────────────
// Renders the skill scores and the hit/miss/false-alarm overlay. A scenario
// with no observed data gets an explicit "no observed outcome" state; it never
// gets an empty scorecard that could be mistaken for a bad score.
async function _loadValidation(jobId) {
  const panel = document.getElementById("validation-panel");
  if (!panel) return;

  let v = null;
  try {
    const r = await fetch(`/api/validation/${jobId}`);
    if (r.ok) v = await r.json();
  } catch (e) {
    console.warn("Validation not available:", e);
  }

  // A scenario with nothing to compare against gets no card at all. An empty
  // scorecard reads as a bad score; an absent one reads as what it is.
  if (!v || !v.available) {
    panel.hidden = true;
    panel.innerHTML = "";
    setValidationVisible(false);
    return;
  }

  // Fetch the overlay geometry in parallel; either may legitimately be absent.
  const [agree, observed] = await Promise.all([
    fetch(`/api/validation/${jobId}/agreement`).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch(`/api/validation/${jobId}/observed`).then(r => r.ok ? r.json() : null).catch(() => null),
  ]);
  if (agree && map.getSource("agreement")) map.getSource("agreement").setData(agree);
  if (observed && map.getSource("observed-extent")) {
    map.getSource("observed-extent").setData(observed);
    currentObservedGeoJSON = observed;
    _updateObservedMarker(observed, v);
  }

  const e = v.extent || {};
  const a = v.areas || {};
  // A null score is undefined, not zero. Render it as a dash.
  const num = (x, dp = 3) =>
    (x === null || x === undefined || Number.isNaN(+x))
      ? '<span class="pop-na">not defined</span>' : (+x).toFixed(dp);
  const km2 = (x) =>
    (x === null || x === undefined) ? "—" : `${(+x).toFixed(1)} km²`;

  const roads = v.roads;
  const roadsHtml = roads ? `
    <div class="val-sub">Road links (observed damage grades)</div>
    <dl class="pop-grid">
      <dt>POD</dt><dd>${num(roads.pod)}</dd>
      <dt>FAR</dt><dd>${roads.far_meaningful === false
        ? '<span class="pop-na">n/a — no undamaged links observed</span>'
        : num(roads.far)}</dd>
      <dt>Hit / Miss / False</dt><dd>${roads.tp} / ${roads.fn} / ${roads.fp}</dd>
      <dt>Scored links</dt><dd>${roads.n_scored} of ${roads.n_observed_total}</dd>
      <dt>Uncertain, excluded</dt><dd>${roads.n_uncertain_excluded}</dd>
    </dl>` : `
    <p class="proxy-note">Road validation unavailable for this run.</p>`;

  // POD is the share of the real flood the model found. Said in words once, so
  // the acronyms below have something to mean.
  const podPct = (e.pod === null || e.pod === undefined || Number.isNaN(+e.pod))
    ? null : Math.round(+e.pod * 100);
  const plain = podPct === null ? "" :
    `<p class="val-plain">Of the area that really flooded, the model found
     <strong>${podPct}%</strong>.</p>`;

  panel.hidden = false;
  panel.innerHTML = `
    <h2>How close we got</h2>
    <p class="val-event">${escapeHtml(v.event || "")}</p>
    ${plain}

    <label class="val-toggle">
      <input type="checkbox" id="val-overlay-toggle" checked
             onchange="setValidationVisible(this.checked)" />
      Show hit / miss / false-alarm overlay
    </label>

    <div class="val-swatches">
      <span><i style="background:${AGREE_COLORS[1]}"></i>Hit</span>
      <span><i style="background:${AGREE_COLORS[2]}"></i>Miss</span>
      <span><i style="background:${AGREE_COLORS[3]}"></i>False alarm</span>
      <span><i class="obs-swatch-box"></i>Observed truth</span>
    </div>

    <button type="button" class="btn-focus-obs" onclick="focusObservedExtent()">
      <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="8" cy="8" r="6"/><circle cx="8" cy="8" r="2"/>
      </svg>
      Focus Observed Ground Truth
    </button>

    <div class="val-sub">Flood extent</div>
    <dl class="pop-grid">
      <dt><strong>CSI</strong></dt><dd><strong>${num(e.csi)}</strong></dd>
      <dt>POD</dt><dd>${num(e.pod)}</dd>
      <dt>FAR</dt><dd>${num(e.far)}</dd>
      <dt>Bias</dt><dd>${num(e.bias)}</dd>
      <dt>Simulated area</dt><dd>${km2(a.simulated_km2)}</dd>
      <dt>Observed area</dt><dd>${km2(a.observed_km2)}</dd>
      <dt>Scored area</dt><dd>${km2(a.scored_km2)}</dd>
    </dl>
    ${roadsHtml}

    <p class="proxy-note">
      Source: ${v.source || "—"} · threshold ${v.threshold_m} m.
      Scoring is clipped to the mapped Area of Interest: outside it there is no
      observation, which is not the same as observed dry.
      <strong>Derna was a compound event</strong> — the observed extent includes
      rainfall flooding as well as the two dam-break waves, and FloodSight routes
      only the dam break, so POD is expected to read low here.
    </p>`;

  setValidationVisible(true);
}

// ── Bootstrap ─────────────────────────────────────────────────────────────
// Fetch real scenario geometry before the map (and its dam-structure layer)
// is built at all, so SCENARIO_GEOMETRY is populated before anything -- e.g.
// the "dam structure" step inside map.on("load") -- tries to read it. Avoids
// a race where the map could render dam structure before geometry loaded.
document.addEventListener("DOMContentLoaded", () => {
  _loadScenarioGeometry().then(initMap);
});

// MapLibre will not load its style while the canvas is hidden, and a canvas
// sized in a zero-height viewport comes back wrong. Both are recorded in
// ui.md as things that look like bugs. Re-measuring and forcing one repaint
// when the tab returns is what actually clears them.
document.addEventListener("visibilitychange", () => {
  if (document.hidden || !map) return;
  map.resize();
  map.triggerRepaint();
});

// ── UI Helpers ────────────────────────────────────────────────────────────
window.updateScenarioDefaults = function() {
  const scenarioKey = document.getElementById("scenario-select").value;
  // The <select> can fire before DOMContentLoaded has built the map.
  const fly = (center, zoom, pitch, bearing) => {
    if (map) {
      map.flyTo({
        center,
        zoom,
        pitch: pitch !== undefined ? pitch : 40,
        bearing: bearing !== undefined ? bearing : -10,
        duration: 1400,
        easing: t => 1 - Math.pow(1 - t, 3)
      });
    }
  };

  // Immediately reset previous simulation state so stale villages/numbers from a prior scenario never linger
  simulationResults = null;
  _invalidateIsolationIndex();
  if (map && map.getSource("villages")) {
    map.getSource("villages").setData(EMPTY_RESULTS);
  }
  _updateVillageMarkers(EMPTY_RESULTS);
  renderPriorityList([]);
  updateCounters([]);
  const priorityListEl = document.getElementById("priority-list");
  if (priorityListEl) {
    priorityListEl.innerHTML =
      '<div class="priority-loading" style="padding:16px;text-align:center;color:var(--text-muted);font-size:12px;"><span class="pulse-dot" style="margin-right:6px"></span>Loading simulation & impact telemetry...</div>';
  }

  // Immediate synchronous updates so the UI never waits on async context layers
  _updateDamMarker(scenarioKey);
  _updateDamStructure(scenarioKey);
  _updateDamSetupCard(scenarioKey);
  _loadContextLayers(scenarioKey);

  const damInput = document.getElementById("dam-name-input");

  if (scenarioKey === "rishiganga") {
    damInput.value = "Rishi Ganga Avalanche Barrier & Cascade (2021)";
    fly([79.712, 30.469], 12.5, 52, 305);
  } else if (scenarioKey === "phutkal") {
    damInput.value = "Phutkal River Landslide Dam 2015";
    fly([77.058, 33.252], 12.8, 50, 320);
  } else if (scenarioKey === "south_lhonak") {
    damInput.value = "South Lhonak Moraine Dam & Chungthang Cascade (2023)";
    fly([88.220, 27.880], 11.0, 54, 120);
  } else if (scenarioKey === "derna") {
    damInput.value = "Derna Dams — Abu Mansour + Al-Bilad Cascade (2023)";
    fly([22.605, 32.705], 12.0, 48, 40);
  } else if (scenarioKey === "ivanovo") {
    damInput.value = "Ivanovo Dam — Biser, Bulgaria (2012)";
    fly([25.868, 41.872], 13.0, 45, 65);
  } else if (scenarioKey === "malpasset") {
    damInput.value = "Malpasset Arch Dam — Reyran Valley, France (1959)";
    fly([6.756, 43.508], 13.2, 52, 170);
  } else if (scenarioKey === "annamayya") {
    damInput.value = "Annamayya Dam Failure & Pincha Cascade (2021)";
    fly([79.021, 14.211], 13.5, 45, 35);
  }

  window.CURRENT_SCENARIO_KEY = scenarioKey;
  window.SCENARIO_PRESENTATION = SCENARIO_PRESENTATION;
  window.CURRENT_DAM = _getDamData(scenarioKey);

  // Automatically load the full simulation results, snapshots, and road network
  _autoLoadScenarioSimulation(scenarioKey);
};

// ── Theme changes ─────────────────────────────────────────────────────────
// The ramps are read from CSS at layer-creation time, so a theme switch has to
// push the new values back into MapLibre — otherwise the map keeps the palette
// it was built with while the rest of the page changes around it.
document.addEventListener("floodsight:themechange", () => {
  if (typeof map === "undefined" || !map || !map.isStyleLoaded()) return;
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  // Re-apply the CURRENT basemap's paint rather than a theme-only guess, so a
  // theme toggle no longer overwrites the styling chosen for satellite/topo.
  _applyBasemapPaint(currentBasemap);
  if (map.getLayer("flood-depth-fill")) {
    map.setPaintProperty("flood-depth-fill", "fill-color", [
      "step", ["get", "depth_class"],
      depthRamp()[0], 1, depthRamp()[0], 2, depthRamp()[1],
      3, depthRamp()[3], 4, depthRamp()[5],
    ]);
  }
  for (const id of ["villages-fill", "villages-outline"]) {
    if (!map.getLayer(id)) continue;
    const prop = id.endsWith("fill") ? "fill-color" : "line-color";
    map.setPaintProperty(id, prop, [
      "interpolate", ["linear"], ["get", "priority_score"],
      0.0, priorityRamp()[0], 0.5, priorityRamp()[1], 1.0, priorityRamp()[2],
    ]);
  }
});

// ── Tactical Map Markings Implementation ──────────────────────────────────
function _updateDamMarker(scenarioKey) {
  if (!map) return;
  const key = scenarioKey || document.getElementById("scenario-select")?.value || "rishiganga";
  const dam = _getDamData(key) || _getDamData("rishiganga");
  if (damMarker) {
    damMarker.remove();
    damMarker = null;
  }
  if (damPopup) {
    damPopup.remove();
  }

  const el = document.createElement("div");
  el.className = "map-mark dam-mark";
  el.title = `${dam.name} — Click to inspect dam & breach telemetry`;
  el.innerHTML = `
    <div class="dam-label">
      <span class="dam-kicker"><span class="dam-kicker-dot"></span>${escapeHtml(dam.breach_short || "BREACH")}</span>
      <span class="dam-name">${escapeHtml(dam.name)}</span>
      <span class="dam-meta">· ${dam.lat.toFixed(3)}°N, ${dam.lon.toFixed(3)}°E</span>
    </div>
    <div class="dam-beacon">
      <div class="dam-beacon-pulse"></div>
      <div class="dam-beacon-pulse"></div>
      <div class="dam-beacon-core">
        <svg viewBox="0 0 20 20" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2">
          <path d="M3 16V9l7-5 7 5v7M3 16h14M7 16V10h6v6" stroke-linecap="round" stroke-linejoin="round"/>
          <line x1="10" y1="4" x2="10" y2="16" stroke="#EF4444" stroke-width="2" stroke-dasharray="2 1"/>
        </svg>
      </div>
    </div>
  `;

  const popupHtml = `
    <div class="dam-inspector-popup">
      <div class="dam-popup-header">
        <div class="dam-popup-tag"><span class="pulse-dot"></span>BREACH ORIGIN &amp; STRUCTURE</div>
        <div class="dam-popup-title">${escapeHtml(dam.name)}</div>
        <div class="dam-popup-subtitle">${escapeHtml(dam.event || dam.type)}</div>
      </div>
      <div class="dam-popup-body">
        <div class="dam-breach-mode-badge">
          <span class="badge-label">FAILURE MECHANISM:</span>
          <span class="badge-val">${escapeHtml(dam.breach_mode || "Progressive Overtopping & Washout")}</span>
        </div>
        <div class="dam-specs-grid">
          <div class="spec-item"><span class="lbl">Dam Type</span><span class="val">${escapeHtml(dam.type)}</span></div>
          <div class="spec-item"><span class="lbl">Dam Height</span><span class="val">${dam.height} m</span></div>
          <div class="spec-item"><span class="lbl">Crest Length</span><span class="val">${dam.crest_length_m ? dam.crest_length_m + " m" : "—"}</span></div>
          <div class="spec-item"><span class="lbl">Crest WSE</span><span class="val">${dam.wse} m</span></div>
          <div class="spec-item"><span class="lbl">Bed Elevation</span><span class="val">${dam.bed_elevation_m ? dam.bed_elevation_m + " m" : "—"}</span></div>
          <div class="spec-item"><span class="lbl">Storage Capacity</span><span class="val">${dam.vol} MCM</span></div>
          <div class="spec-item"><span class="lbl">Peak Outflow Q<sub>p</sub></span><span class="val">${dam.q_peak || "—"}</span></div>
          <div class="spec-item"><span class="lbl">Coordinates</span><span class="val">${dam.lat.toFixed(4)}°N, ${dam.lon.toFixed(4)}°E</span></div>
        </div>
      </div>
      <div class="dam-popup-footer">
        <button type="button" class="btn btn-sm btn-primary" onclick="focusDamBreachPoint('${key}')">
          <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2"><circle cx="8" cy="8" r="6"/><circle cx="8" cy="8" r="2"/></svg>
          Zoom 3D Canyon View
        </button>
      </div>
    </div>
  `;

  damPopup = new maplibregl.Popup({ className: "dam-telemetry-popup-container", offset: [0, -36], closeButton: true, maxWidth: "340px" })
    .setLngLat([dam.lon, dam.lat])
    .setHTML(popupHtml);

  el.addEventListener("click", (e) => {
    e.stopPropagation();
    damPopup.addTo(map);
    map.flyTo({
      center: [dam.lon, dam.lat],
      zoom: dam.focus_zoom || 14.2,
      pitch: dam.focus_pitch || 48,
      bearing: dam.focus_bearing || -15,
      duration: 1200
    });
  });

  damMarker = new maplibregl.Marker({ element: el, anchor: "bottom" })
    .setLngLat([dam.lon, dam.lat])
    .addTo(map);

  if (cascadeMarker) {
    cascadeMarker.remove();
    cascadeMarker = null;
  }
  if (cascadePopup) {
    cascadePopup.remove();
    cascadePopup = null;
  }
  if (preDamMarker) {
    preDamMarker.remove();
    preDamMarker = null;
  }
  if (preDamPopup) {
    preDamPopup.remove();
    preDamPopup = null;
  }
  if (subDamMarker) {
    subDamMarker.remove();
    subDamMarker = null;
  }
  if (subDamPopup) {
    subDamPopup.remove();
    subDamPopup = null;
  }

  // Cascading structure markers (e.g. Pincha Pre-Dam -> Annamayya Dam, Abu Mansour -> Al-Bilad)
  if (dam.cascade) {
    const c = dam.cascade;

    // 1. Upstream Pre-Dam Marker (if distinct from breach dam)
    if (c.upstream_coords && (Math.abs(c.upstream_coords[0] - dam.lon) > 0.005 || Math.abs(c.upstream_coords[1] - dam.lat) > 0.005)) {
      const pel = document.createElement("div");
      pel.className = "map-mark cascade-mark predam-mark";
      pel.title = `${c.upstream_name} — Upstream trigger / pre-dam failure`;
      pel.innerHTML = `
        <div class="dam-label" style="border-left-color:#f59e0b">
          <div class="dam-kicker"><span class="dam-kicker-dot" style="background:#f59e0b"></span>PRE-DAM · ${escapeHtml(c.upstream_status.toUpperCase())}</div>
          <div class="dam-name">${escapeHtml(c.upstream_name)}</div>
          <div class="dam-meta">Trigger Origin · ${c.upstream_coords[1].toFixed(3)}°N, ${c.upstream_coords[0].toFixed(3)}°E</div>
        </div>
        <div class="dam-beacon">
          <div class="dam-beacon-pulse" style="border-color:#f59e0b"></div>
          <div class="dam-beacon-pulse" style="border-color:#f59e0b"></div>
          <div class="dam-beacon-core" style="background:rgba(245,158,11,0.25);border-color:#f59e0b;color:#f59e0b">
            <svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2">
              <path d="M10 3v14M3 10l7-7 7 7" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </div>
        </div>
      `;

      preDamPopup = new maplibregl.Popup({ className: "dam-telemetry-popup-container", offset: [0, -36], closeButton: true, maxWidth: "340px" })
        .setLngLat(c.upstream_coords)
        .setHTML(`
          <div class="dam-inspector-popup">
            <div class="dam-popup-header">
              <div class="dam-popup-tag" style="color:#f59e0b"><span class="pulse-dot" style="background:#f59e0b"></span>UPSTREAM PRE-FAILURE TRIGGER</div>
              <div class="dam-popup-title">${escapeHtml(c.upstream_name)}</div>
              <div class="dam-popup-subtitle">${escapeHtml(c.type)}</div>
            </div>
            <div class="dam-popup-body">
              <div class="dam-breach-mode-badge" style="border-color:rgba(245,158,11,0.4);background:rgba(245,158,11,0.1)">
                <span class="badge-label">TRIGGER STATUS:</span>
                <span class="badge-val" style="color:#f59e0b">${escapeHtml(c.upstream_status)}</span>
              </div>
              <p style="font-size:12px;line-height:1.5;margin-top:8px;color:var(--text)">${escapeHtml(c.chain_desc)}</p>
            </div>
            <div class="dam-popup-footer">
              <button type="button" class="btn btn-sm btn-primary" onclick="map.flyTo({center:[${c.upstream_coords[0]},${c.upstream_coords[1]}],zoom:13.8,pitch:45,duration:1200})">
                Focus Pre-Dam Site
              </button>
            </div>
          </div>
        `);

      pel.addEventListener("click", (e) => {
        e.stopPropagation();
        preDamPopup.addTo(map);
        map.flyTo({ center: c.upstream_coords, zoom: 13.8, pitch: 45, duration: 1200 });
      });

      preDamMarker = new maplibregl.Marker({ element: pel, anchor: "bottom" })
        .setLngLat(c.upstream_coords)
        .addTo(map);

      if (!mapMarkingsVisible) pel.style.display = "none";
    }

    // 2. Midstream Sub-Dam Marker (e.g. Rishiganga HEP)
    if (c.subdam_coords) {
      const sel = document.createElement("div");
      sel.className = "map-mark cascade-mark subdam-mark";
      sel.title = `${c.subdam_name} — Cascading hydro structure telemetry`;
      sel.innerHTML = `
        <div class="dam-label" style="border-left-color:#f59e0b">
          <div class="dam-kicker"><span class="dam-kicker-dot" style="background:#f59e0b"></span>SUB-DAM · ${escapeHtml(c.subdam_status.toUpperCase())}</div>
          <div class="dam-name">${escapeHtml(c.subdam_name)}</div>
          <div class="dam-meta">Cascade Point · ${c.subdam_coords[1].toFixed(3)}°N, ${c.subdam_coords[0].toFixed(3)}°E</div>
        </div>
        <div class="dam-beacon">
          <div class="dam-beacon-pulse" style="border-color:#f59e0b"></div>
          <div class="dam-beacon-pulse" style="border-color:#f59e0b"></div>
          <div class="dam-beacon-core" style="background:rgba(245,158,11,0.25);border-color:#f59e0b;color:#f59e0b">
            <svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2">
              <path d="M4 14l6-6 6 6M4 9l6-6 6 6" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </div>
        </div>
      `;

      subDamPopup = new maplibregl.Popup({ className: "dam-telemetry-popup-container", offset: [0, -36], closeButton: true, maxWidth: "340px" })
        .setLngLat(c.subdam_coords)
        .setHTML(`
          <div class="dam-inspector-popup">
            <div class="dam-popup-header">
              <div class="dam-popup-tag" style="color:#f59e0b"><span class="pulse-dot" style="background:#f59e0b"></span>CASCADE HYDRO SUB-DAM</div>
              <div class="dam-popup-title">${escapeHtml(c.subdam_name)}</div>
            </div>
            <div class="dam-popup-body">
              <div class="dam-breach-mode-badge" style="border-color:rgba(245,158,11,0.4);background:rgba(245,158,11,0.1)">
                <span class="badge-label">STATUS:</span>
                <span class="badge-val" style="color:#f59e0b">${escapeHtml(c.subdam_status)}</span>
              </div>
            </div>
            <div class="dam-popup-footer">
              <button type="button" class="btn btn-sm btn-primary" onclick="map.flyTo({center:[${c.subdam_coords[0]},${c.subdam_coords[1]}],zoom:13.8,pitch:45,duration:1200})">
                Focus Sub-Dam Site
              </button>
            </div>
          </div>
        `);

      sel.addEventListener("click", (e) => {
        e.stopPropagation();
        subDamPopup.addTo(map);
        map.flyTo({ center: c.subdam_coords, zoom: 13.8, pitch: 45, duration: 1200 });
      });

      subDamMarker = new maplibregl.Marker({ element: sel, anchor: "bottom" })
        .setLngLat(c.subdam_coords)
        .addTo(map);

      if (!mapMarkingsVisible) sel.style.display = "none";
    }

    // 3. Downstream Cascade Structure Marker (e.g. Al-Bilad, Tapovan Barrage, Chungthang Dam)
    if (c.downstream_coords) {
      const cel = document.createElement("div");
      cel.className = "map-mark cascade-mark";
      cel.title = `${c.downstream_name} — Cascading structure telemetry`;
      cel.innerHTML = `
        <div class="dam-label" style="border-left-color:#f59e0b">
          <div class="dam-kicker"><span class="dam-kicker-dot" style="background:#f59e0b"></span>CASCADE · ${escapeHtml(c.downstream_status.toUpperCase())}</div>
          <div class="dam-name">${escapeHtml(c.downstream_name)}</div>
          <div class="dam-meta">Transit: T+${c.transit_min || 30} min · Reach: ${c.distance_km || 0} km</div>
        </div>
        <div class="dam-beacon">
          <div class="dam-beacon-pulse" style="border-color:#f59e0b"></div>
          <div class="dam-beacon-pulse" style="border-color:#f59e0b"></div>
          <div class="dam-beacon-core" style="background:rgba(245,158,11,0.25);border-color:#f59e0b;color:#f59e0b">
            <svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2">
              <path d="M4 14l6-6 6 6M4 9l6-6 6 6" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </div>
        </div>
      `;

      const cPopupHtml = `
        <div class="dam-inspector-popup">
          <div class="dam-popup-header">
            <div class="dam-popup-tag" style="color:#f59e0b"><span class="pulse-dot" style="background:#f59e0b"></span>CASCADING FAILURE CHAIN</div>
            <div class="dam-popup-title">${escapeHtml(c.downstream_name)}</div>
            <div class="dam-popup-subtitle">${escapeHtml(c.type)}</div>
          </div>
          <div class="dam-popup-body">
            <div class="dam-breach-mode-badge" style="border-color:rgba(245,158,11,0.4);background:rgba(245,158,11,0.1)">
              <span class="badge-label">STATUS:</span>
              <span class="badge-val" style="color:#f59e0b">${escapeHtml(c.downstream_status)}</span>
            </div>
            <p style="font-size:12px;line-height:1.5;margin-top:8px;color:var(--text)">${escapeHtml(c.chain_desc)}</p>
            <div class="dam-specs-grid" style="margin-top:8px">
              <div class="spec-item"><span class="lbl">Surge Reach</span><span class="val">${c.distance_km || 0} km</span></div>
              <div class="spec-item"><span class="lbl">Transit Time</span><span class="val">T+${c.transit_min || 0} min</span></div>
              <div class="spec-item"><span class="lbl">Upstream Inflow</span><span class="val">${escapeHtml(c.upstream_name || dam.name)}</span></div>
              <div class="spec-item"><span class="lbl">Coordinates</span><span class="val">${c.downstream_coords[1].toFixed(4)}°N, ${c.downstream_coords[0].toFixed(4)}°E</span></div>
            </div>
          </div>
          <div class="dam-popup-footer">
            <button type="button" class="btn btn-sm btn-primary" onclick="map.flyTo({center:[${c.downstream_coords[0]},${c.downstream_coords[1]}],zoom:13.8,pitch:45,duration:1200})">
              Focus Downstream Structure
            </button>
          </div>
        </div>
      `;

      cascadePopup = new maplibregl.Popup({ className: "dam-telemetry-popup-container", offset: [0, -36], closeButton: true, maxWidth: "340px" })
        .setLngLat(c.downstream_coords)
        .setHTML(cPopupHtml);

      cel.addEventListener("click", (e) => {
        e.stopPropagation();
        cascadePopup.addTo(map);
        map.flyTo({
          center: c.downstream_coords,
          zoom: 13.8,
          pitch: 45,
          bearing: -10,
          duration: 1200
        });
      });

      cascadeMarker = new maplibregl.Marker({ element: cel, anchor: "bottom" })
        .setLngLat(c.downstream_coords)
        .addTo(map);

      if (!mapMarkingsVisible) {
        cel.style.display = "none";
      }
    }
  }

  if (!mapMarkingsVisible) {
    el.style.display = "none";
  }
}

window.focusDamBreachPoint = function(scenarioKey) {
  if (!map) return;
  const key = scenarioKey || document.getElementById("scenario-select")?.value || "rishiganga";
  const dam = _getDamData(key) || _getDamData("rishiganga");
  if (!dam) return;

  _updateDamMarker(key);
  _updateDamStructure(key);
  _updateDamSetupCard(key);

  if (damPopup) {
    damPopup.addTo(map);
  }

  map.flyTo({
    center: [dam.lon, dam.lat],
    zoom: dam.focus_zoom || 14.5,
    pitch: dam.focus_pitch || 50,
    bearing: dam.focus_bearing || -15,
    duration: 1400,
    easing: t => 1 - Math.pow(1 - t, 3)
  });
};

function _updateVillageMarkers(geojson) {
  if (!map) return;
  // Clear existing village markers
  for (const m of villageMarkers) m.remove();
  villageMarkers = [];

  const features = (geojson && geojson.features) || [];
  if (!features.length) return;

  const isZoomedIn = map.getZoom() >= 10.2;

  // Sort features: priority rank first
  const sorted = [...features].sort((a, b) => {
    const rA = a.properties?.priority_rank || 999;
    const rB = b.properties?.priority_rank || 999;
    return rA - rB;
  });

  sorted.forEach((f, idx) => {
    const p = f.properties || {};
    let lon = p.lon, lat = p.lat;
    if (isMissing(lon) || isMissing(lat)) {
      if (f.geometry && f.geometry.coordinates && f.geometry.coordinates[0]) {
        const ring = f.geometry.coordinates[0];
        let sumX = 0, sumY = 0;
        for (const pt of ring) { sumX += pt[0]; sumY += pt[1]; }
        lon = sumX / ring.length;
        lat = sumY / ring.length;
      } else {
        return;
      }
    }

    const rank = p.priority_rank;
    const isRank1 = rank === 1;
    const isoMin = p.isolation_time_min;
    const isIsolated = !isMissing(isoMin);

    // The badge rule lives in village_status.js so a test can assert on the
    // real function. It used to be inline here and keyed on arrival time alone
    // (`waterMin < 120`), which put a green SAFE on six settlements that were
    // under water -- Paparajupalle at 3.78 m among them. Danger is depth.
    const { statusClass, statusText, atRisk: isAtRisk } = villageStatus(p);

    // Settlements this run never wetted are not drawn. They carry no operational
    // instruction — nobody acts on "no water here" — and on the corrected
    // exposure there are only two of them, so they were pure clutter.
    //
    // The cost, stated because INVARIANTS.md records the rule this bends:
    // "absence of a marker is absence of evaluation". An empty patch of map now
    // means either "scored, and dry" or "never scored" — and the affected area
    // holds well over a hundred named villages against this layer's 23. The
    // distinction survives in the ranked list, which still carries every
    // scored settlement including the dry ones, and in `results.geojson`.
    // It is gone from the map only.
    if (statusClass === "is-dry") return;

    // At overview zoom, only Rank #1 shows the red evacuation badge to prevent map clutter;
    // other villages render as crisp tactical dots that reveal their badge on hover or high zoom
    const isKeyVillage = isRank1 || (isZoomedIn && (rank <= 3 || isIsolated));
    const dotOnlyClass = !isKeyVillage ? "is-dot-only" : "";

    const el = document.createElement("div");
    el.className = `map-mark village-mark ${statusClass} ${dotOnlyClass}`;
    el.title = `${p.village_name || "Village"} — Click for evacuation timeline`;
    if (p.village_id) el.dataset.villageId = p.village_id;
    el.innerHTML = `
      <div class="village-dot"><div class="arrival-ring"></div></div>
      <div class="village-badge">
        <span class="village-name" dir="auto">${escapeHtml(p.village_name || "Village")}</span>
        ${statusText ? `<span class="village-status">${statusText}</span>` : ""}
      </div>
    `;

    el.addEventListener("click", (e) => {
      e.stopPropagation();
      map.flyTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), 11.5), duration: 1000 });
      showIsolationCallout(p);
    });

    const m = new maplibregl.Marker({ element: el, anchor: "center" })
      .setLngLat([lon, lat])
      .addTo(map);

    villageMarkers.push(m);
  });
}

function _updateWavefrontMarker(frame, tMin) {
  if (!map) return;
  if (!mapMarkingsVisible || tMin < 0 || !frame || !frame.geojson || !frame.geojson.features || !frame.geojson.features.length) {
    if (wavefrontMarker) {
      wavefrontMarker.remove();
      wavefrontMarker = null;
    }
    return;
  }

  const scenarioKey = document.getElementById("scenario-select")?.value || "rishiganga";
  const dam = _getDamData(scenarioKey) || _getDamData("rishiganga");

  if (!frame._wavefrontData) {
    let maxDist = 0;
    let wavePt = null;
    let maxDepth = 0;

    for (const f of frame.geojson.features) {
      const dClass = f.properties?.depth_class || 1;
      const dM = f.properties?.depth_lo_m || (dClass * 0.5);
      if (dM > maxDepth) maxDepth = dM;

      const coords = f.geometry?.coordinates;
      if (!coords) continue;

      const testRing = (ring) => {
        const step = ring.length > 80 ? 3 : 1;
        for (let i = 0; i < ring.length; i += step) {
          const pt = ring[i];
          const d = Math.hypot(pt[0] - dam.lon, pt[1] - dam.lat);
          if (d > maxDist) {
            maxDist = d;
            wavePt = pt;
          }
        }
      };

      if (f.geometry.type === "Polygon") {
        testRing(coords[0]);
      } else if (f.geometry.type === "MultiPolygon") {
        for (const poly of coords) testRing(poly[0]);
      }
    }
    frame._wavefrontData = { wavePt, maxDist, maxDepth };
  }

  if (tMin < 0) {
    if (wavefrontMarker) {
      wavefrontMarker.remove();
      wavefrontMarker = null;
    }
    return;
  }

  const { wavePt, maxDist, maxDepth } = frame._wavefrontData;

  if (!wavePt || maxDist < 0.005) {
    if (wavefrontMarker) {
      wavefrontMarker.remove();
      wavefrontMarker = null;
    }
    return;
  }

  const t = Math.round(tMin || 0);

  if (!wavefrontMarker) {
    const el = document.createElement("div");
    el.className = "map-mark wavefront-mark";
    el.innerHTML = `
      <div class="wavefront-pin">
        <div class="wavefront-ring"></div>
        <div class="wavefront-core"></div>
      </div>
      <div class="wavefront-badge">
        <span class="wavefront-title">
          <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M2 10q3-4 6 0t6 0" stroke-linecap="round"/>
          </svg>
          WAVE FRONT · T+<span id="wave-t">${t}</span> min
        </span>
        <span class="wavefront-sub" id="wave-depth">Max Depth ~${maxDepth.toFixed(1)}m</span>
      </div>
    `;
    wavefrontMarker = new maplibregl.Marker({ element: el, anchor: "center" })
      .setLngLat(wavePt)
      .addTo(map);
  } else {
    wavefrontMarker.setLngLat(wavePt);
    const tSpan = document.getElementById("wave-t");
    if (tSpan) tSpan.textContent = String(t);
    const dSpan = document.getElementById("wave-depth");
    if (dSpan) dSpan.textContent = `Max Depth ~${maxDepth.toFixed(1)}m`;
  }
  _clampWavefrontCallout();
}

// Hides the wavefront callout when it would sit under the left/right panels
// or the spine -- those are opaque glass chrome above the map's z-index, so a
// marker drawn there is not just crowded, it is actually covered.
function _clampWavefrontCallout() {
  if (!wavefrontMarker) return;
  const el = wavefrontMarker.getElement();
  if (!el) return;
  const r = el.getBoundingClientRect();
  if (!r.width) return;
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const covered = ["left-panel", "right-panel", "spine"].some((id) => {
    const panel = document.getElementById(id);
    if (!panel || panel.offsetParent === null) return false;
    const p = panel.getBoundingClientRect();
    return cx >= p.left && cx <= p.right && cy >= p.top && cy <= p.bottom;
  });
  el.style.visibility = covered ? "hidden" : "";
}

let surgeWaveMarker = null;

function _updateSurgeWaveMarker(tMin, scenarioKey) {
  if (!map) return;
  const currentKey = scenarioKey || document.getElementById("scenario-select")?.value || "";
  const dam = _getDamData(currentKey);
  if (!mapMarkingsVisible || currentKey !== "annamayya" || !dam || !dam.cascade || tMin >= 0 || tMin < -160) {
    if (surgeWaveMarker) {
      surgeWaveMarker.remove();
      surgeWaveMarker = null;
    }
    return;
  }

  // Pincha ring bund washed out at T-150 min (03:15 IST, EVD-04).
  // Wave travels 34 km down Cheyyeru to Annamayya, arriving ~T-30 to T-15 min (EVD-07, 105-135 min travel time).
  const tArrival = -25.0; // reaches Annamayya reservoir at ~T-25
  const tStart = -150.0;
  const progress = Math.max(0, Math.min(1, (tMin - tStart) / (tArrival - tStart)));

  // Coordinate interpolation along the Cheyyeru corridor from Pincha [78.99956, 13.90890] to Annamayya [79.02128, 14.21059]
  const pincha = dam.cascade.upstream_coords || [78.99956, 13.90890];
  const annamayya = [dam.lon, dam.lat];
  const midLon = 79.0120, midLat = 14.0600;
  let curLon, curLat;
  if (progress < 0.5) {
    const p2 = progress * 2.0;
    curLon = pincha[0] + (midLon - pincha[0]) * p2;
    curLat = pincha[1] + (midLat - pincha[1]) * p2;
  } else {
    const p2 = (progress - 0.5) * 2.0;
    curLon = midLon + (annamayya[0] - midLon) * p2;
    curLat = midLat + (annamayya[1] - midLat) * p2;
  }

  const t = Math.round(tMin);
  const distKm = (progress * 34.0).toFixed(1);

  if (!surgeWaveMarker) {
    const el = document.createElement("div");
    el.className = "map-mark surge-wave-mark";
    el.innerHTML = `
      <div class="surge-wave-pin" style="display:flex;align-items:center;justify-content:center;position:relative">
        <div class="surge-wave-ring" style="position:absolute;width:28px;height:28px;border-radius:50%;border:2px solid #f59e0b;animation:pinchaPulse 2s ease-out infinite"></div>
        <div class="surge-wave-core" style="width:12px;height:12px;border-radius:50%;background:#f59e0b;box-shadow:0 0 10px #f59e0b"></div>
      </div>
      <div class="surge-wave-badge" style="background:rgba(15,23,42,0.94);border:1px solid #f59e0b;color:#f8fafc;padding:5px 9px;border-radius:6px;font-size:11px;white-space:nowrap;box-shadow:0 4px 14px rgba(0,0,0,0.6);margin-top:6px">
        <div style="font-weight:700;color:#f59e0b;display:flex;align-items:center;gap:4px">
          <span style="width:7px;height:7px;border-radius:50%;background:#f59e0b;display:inline-block"></span>
          PINCHA SURGE EN ROUTE (EVD-07)
        </div>
        <div style="color:#94a3b8;font-size:10px;margin-top:2px">
          T<span id="surge-t">${t}</span> min · Cheyyeru <span id="surge-km">${distKm}</span> / 34 km
        </div>
      </div>
    `;
    surgeWaveMarker = new maplibregl.Marker({ element: el, anchor: "center" })
      .setLngLat([curLon, curLat])
      .addTo(map);
  } else {
    surgeWaveMarker.setLngLat([curLon, curLat]);
    const tSpan = document.getElementById("surge-t");
    if (tSpan) tSpan.textContent = String(t);
    const kmSpan = document.getElementById("surge-km");
    if (kmSpan) kmSpan.textContent = distKm;
  }
}

let _roadCutPool = [];
let _lastRoadCutT = -99999;

function _clearRoadCutPool() {
  for (const item of _roadCutPool) {
    if (item.marker) item.marker.remove();
  }
  _roadCutPool = [];
  roadCutMarkers = [];
  _lastRoadCutT = -99999;
}

function _updateRoadCutMarkers(tMin) {
  if (!map) return;
  if (!mapMarkingsVisible) {
    for (let i = 0; i < _roadCutPool.length; i++) {
      _roadCutPool[i].el.style.display = "none";
    }
    roadCutMarkers = [];
    return;
  }
  if (!roadsGeoJSON || !roadsGeoJSON.features) return;
  const t = Math.round(tMin || 0);
  if (t === _lastRoadCutT) return;
  _lastRoadCutT = t;

  if (t < 0) {
    for (let i = 0; i < _roadCutPool.length; i++) {
      _roadCutPool[i].el.style.display = "none";
    }
    roadCutMarkers = [];
    return;
  }

  if (!roadsGeoJSON._cutFeatures) {
    roadsGeoJSON._cutFeatures = (roadsGeoJSON.features || [])
      .filter(f => !isMissing(f.properties?.cut_time_min))
      .sort((a, b) => (+a.properties.cut_time_min) - (+b.properties.cut_time_min));
  }

  // Binary search for upper bound index where cut_time_min <= t
  const cuts = roadsGeoJSON._cutFeatures;
  let low = 0, high = cuts.length;
  while (low < high) {
    const mid = (low + high) >>> 1;
    if (+cuts[mid].properties.cut_time_min <= t) {
      low = mid + 1;
    } else {
      high = mid;
    }
  }
  // On a dense graph (Annamayya: 355 cut links) a marker per link is
  // unreadable clutter. Cap at the ~40 most important VISIBLE ones: major
  // highway classes first, and only links whose midpoint is on screen.
  const ROADCUT_CAP = 15;
  const MAJOR_CLASS = new Set(["trunk", "primary", "secondary", "tertiary"]);
  const bounds = map.getBounds();
  const eligible = cuts.slice(0, low).filter((f) => {
    const coords = f.geometry?.coordinates;
    if (!coords || !coords.length) return false;
    const pt = coords[Math.floor(coords.length / 2)];
    return bounds.contains(pt);
  });
  eligible.sort((a, b) => {
    const am = MAJOR_CLASS.has(a.properties?.highway) ? 0 : 1;
    const bm = MAJOR_CLASS.has(b.properties?.highway) ? 0 : 1;
    if (am !== bm) return am - bm;
    return (+b.properties.cut_time_min) - (+a.properties.cut_time_min); // most recent first
  });
  const displayed = eligible.slice(0, ROADCUT_CAP);

  for (let i = 0; i < displayed.length; i++) {
    const f = displayed[i];
    const coords = f.geometry?.coordinates;
    if (!coords || !coords.length) continue;
    const midIdx = Math.floor(coords.length / 2);
    const pt = coords[midIdx];
    const isBridge = !!f.properties?.is_bridge;
    const cutT = Math.round(f.properties?.cut_time_min || 0);
    const key = `${isBridge ? "b" : "r"}_${cutT}_${pt[0].toFixed(5)}_${pt[1].toFixed(5)}`;

    let item = _roadCutPool[i];
    if (!item) {
      const el = document.createElement("div");
      el.className = `map-mark roadcut-mark ${isBridge ? "is-bridge" : ""}`;
      const marker = new maplibregl.Marker({ element: el, anchor: "center" })
        .setLngLat(pt)
        .addTo(map);
      item = { marker, el, key: "" };
      _roadCutPool.push(item);
    }

    if (item.key !== key) {
      item.key = key;
      item.el.className = `map-mark roadcut-mark ${isBridge ? "is-bridge" : ""}`;
      item.el.title = isBridge
        ? `Bridge lost at T+${cutT} min`
        : `Road cut at T+${cutT} min (depth ≥ threshold)`;
      item.el.innerHTML = `
        <div class="roadcut-pin">
          ${isBridge ? `
            <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M2 11V6a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v5M2 11h12M5 11V8m6 3V8"/>
            </svg>
          ` : `
            <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="8" cy="8" r="6"/>
              <path d="M4.5 8h7"/>
            </svg>
          `}
        </div>
        <div class="roadcut-badge">
          <span>${isBridge ? "BRIDGE CUT" : "ROAD CUT"}</span>
          <em>T+${cutT}m</em>
        </div>
      `;
      item.marker.setLngLat(pt);
    } else {
      item.marker.setLngLat(pt);
    }

    item.el.style.display = mapMarkingsVisible ? "" : "none";
  }

  for (let i = displayed.length; i < _roadCutPool.length; i++) {
    _roadCutPool[i].el.style.display = "none";
  }

  roadCutMarkers = _roadCutPool.slice(0, displayed.length).map(p => p.marker);
}

let _lastObservedMeta = null;

function _updateObservedMarker(geojson, meta) {
  if (!map) return;
  if (observedMarker) {
    observedMarker.remove();
    observedMarker = null;
  }
  if (!geojson || !geojson.features || !geojson.features.length) return;
  if (meta !== undefined) _lastObservedMeta = meta || null;
  meta = meta !== undefined ? meta : _lastObservedMeta;
  // No backend-vouched source metadata: draw nothing rather than a guessed tag.
  if (!meta || !meta.source) return;

  // Compute centroid of the largest or primary feature
  let sumX = 0, sumY = 0, count = 0;
  let bestCoords = null;
  let bestCount = 0;

  for (const f of geojson.features) {
    const geom = f.geometry;
    if (!geom) continue;
    let featCoords = [];
    if (geom.type === "Polygon" && geom.coordinates && geom.coordinates[0]) {
      featCoords = geom.coordinates[0];
    } else if (geom.type === "MultiPolygon" && geom.coordinates && geom.coordinates[0]) {
      featCoords = geom.coordinates[0][0];
    } else if (geom.type === "Point") {
      featCoords = [geom.coordinates];
    }
    if (featCoords.length > bestCount) {
      bestCount = featCoords.length;
      bestCoords = featCoords;
    }
  }

  if (bestCoords && bestCoords.length) {
    for (const pt of bestCoords) {
      sumX += pt[0];
      sumY += pt[1];
      count++;
    }
  }

  if (!count) return;

  const centerLng = sumX / count;
  const centerLat = sumY / count;

  // Provenance text comes only from the backend's own describe() response
  // (served at /api/observed/scenarios) — never guessed from the scenario key.
  const tagText = meta.classification || "OBSERVED";
  const sourceText = meta.source;
  const eventText = meta.event || "Observed Inundation";

  const el = document.createElement("div");
  el.className = "map-mark observed-mark";
  el.title = `Observed Ground Truth (${sourceText}) — Click to focus observed extent`;
  el.innerHTML = `
    <div class="observed-pin">
      <div class="observed-ring"></div>
      <div class="observed-core">
        <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="8" cy="8" r="3"/>
          <path d="M2.5 8a5.5 5.5 0 0 1 11 0M1 8a7 7 0 0 1 14 0" stroke-linecap="round"/>
        </svg>
      </div>
    </div>
    <div class="observed-badge">
      <div class="observed-kicker">
        <span class="observed-kicker-dot"></span>
        OBSERVED GROUND TRUTH
      </div>
      <div class="observed-sub">
        <span>${escapeHtml(eventText)}</span>
        <span class="observed-tag">${escapeHtml(tagText)}</span>
      </div>
    </div>
  `;

  el.addEventListener("click", (e) => {
    e.stopPropagation();
    map.flyTo({ center: [centerLng, centerLat], zoom: Math.max(map.getZoom(), 11.2), duration: 1200 });
  });

  observedMarker = new maplibregl.Marker({ element: el, anchor: "center" })
    .setLngLat([centerLng, centerLat])
    .addTo(map);

  if (!mapMarkingsVisible) {
    el.style.display = "none";
  }
}

window.focusObservedExtent = function() {
  if (observedMarker) {
    const ll = observedMarker.getLngLat();
    map.flyTo({ center: [ll.lng, ll.lat], zoom: Math.max(map.getZoom(), 11.5), duration: 1200 });
  } else if (currentObservedGeoJSON && currentObservedGeoJSON.features && currentObservedGeoJSON.features.length) {
    _updateObservedMarker(currentObservedGeoJSON);
    if (observedMarker) {
      const ll = observedMarker.getLngLat();
      map.flyTo({ center: [ll.lng, ll.lat], zoom: Math.max(map.getZoom(), 11.5), duration: 1200 });
    }
  }
};

window.toggleMapMarkings = function() {
  mapMarkingsVisible = !mapMarkingsVisible;
  document.body.classList.toggle("hide-markings", !mapMarkingsVisible);
  const btn = document.getElementById("btn-toggle-markings");
  if (btn) {
    btn.classList.toggle("is-active", mapMarkingsVisible);
    btn.setAttribute("aria-pressed", mapMarkingsVisible ? "true" : "false");
  }
};

// ── Flow-field particle overlay (run_layer/front_field) ─────────────────────
// A regular lon/lat grid of (u, v) unit vectors -- the gradient of the run's
// own arrival-time field, i.e. which way the front was travelling, not a
// velocity. Rendered as drifting particles on a plain 2D canvas so the cost
// stays flat regardless of zoom: MapLibre's WebGL context is never touched.
const FLOW_PARTICLE_COUNT = 1600;
let flowCanvas = null, flowCtx = null;
let flowParticles = null;   // typed arrays: x, y (screen px), age, seedCol, seedRow
let flowRafId = null;
let flowLastFrameT = 0;

function _initFlowFieldCanvas() {
  flowCanvas = document.getElementById("flow-canvas");
  if (!flowCanvas) return;
  flowCtx = flowCanvas.getContext("2d");
  _resizeFlowCanvas();
  window.addEventListener("resize", _resizeFlowCanvas);
  map.on("move", () => { if (flowParticles) _seedFlowParticles(); });
  map.on("moveend", () => { if (flowParticles) _seedFlowParticles(); });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { _stopFlowLoop(); }
    else if (flowFieldOn && frontField) { _startFlowLoop(); }
  });
}

function _resizeFlowCanvas() {
  if (!flowCanvas) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  flowCanvas.width = Math.round(window.innerWidth * dpr);
  flowCanvas.height = Math.round(window.innerHeight * dpr);
  flowCanvas.style.width = window.innerWidth + "px";
  flowCanvas.style.height = window.innerHeight + "px";
  if (flowCtx) flowCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

async function _loadFrontField(jobId) {
  frontField = null;
  try {
    const r = await fetch(`/api/run_layer/${jobId}/front_field`);
    if (!r.ok) return;
    frontField = await r.json();
    if (frontField && flowFieldOn) {
      _seedFlowParticles();
      _startFlowLoop();
    }
  } catch (e) {
    console.warn("front_field not available:", e);
  }
}

// Sample (u, v, t_arr_min) at the nearest grid cell. Grid rows are stored
// NORTH row first, so row index grows southward -- the opposite of latitude.
function _sampleFrontField(lon, lat) {
  const f = frontField;
  if (!f || !f.nx || !f.ny) return null;
  const col = Math.round(((lon - f.west) / (f.east - f.west)) * (f.nx - 1));
  const rowFromNorth = Math.round(((f.north - lat) / (f.north - f.south)) * (f.ny - 1));
  if (col < 0 || col >= f.nx || rowFromNorth < 0 || rowFromNorth >= f.ny) return null;
  const idx = rowFromNorth * f.nx + col;
  const u = f.u[idx], v = f.v[idx], t = f.t_arr_min[idx];
  if (u === null || u === undefined || v === null || v === undefined) return null;
  return { u, v, t_arr_min: (t === null || t === undefined) ? null : t };
}

function _seedFlowParticles() {
  if (!frontField || !map) return;
  const n = FLOW_PARTICLE_COUNT;
  if (!flowParticles || flowParticles.x.length !== n) {
    flowParticles = {
      x: new Float32Array(n), y: new Float32Array(n),
      age: new Float32Array(n), life: new Float32Array(n),
    };
  }
  const b = map.getBounds();
  const west = Math.max(frontField.west, b.getWest());
  const east = Math.min(frontField.east, b.getEast());
  const south = Math.max(frontField.south, b.getSouth());
  const north = Math.min(frontField.north, b.getNorth());
  for (let i = 0; i < n; i++) _respawnParticle(i, west, east, south, north);
}

const _flowRespawnBounds = { west: 0, east: 0, south: 0, north: 0 };
function _respawnParticle(i, west, east, south, north) {
  // Only spawn where the front has already arrived by the current sim time
  // (t_arr_min <= t); retried a handful of times before giving up on this
  // tick so a mostly-dry frame does not spin forever.
  for (let attempt = 0; attempt < 8; attempt++) {
    const lon = west + Math.random() * (east - west);
    const lat = south + Math.random() * (north - south);
    const s = _sampleFrontField(lon, lat);
    if (!s || s.t_arr_min === null) continue;
    if (s.t_arr_min > flowLastFrameT) continue;
    const pt = map.project([lon, lat]);
    flowParticles.x[i] = pt.x;
    flowParticles.y[i] = pt.y;
    flowParticles.age[i] = 0;
    flowParticles.life[i] = 40 + Math.random() * 40;
    return;
  }
  // Nothing wet found nearby this attempt -- park it off-screen; the next
  // tick's respawn pass will retry it once more water has arrived.
  flowParticles.x[i] = -9999;
  flowParticles.y[i] = -9999;
  flowParticles.age[i] = flowParticles.life[i] || 1;
}

const FLOW_SPEED_PX_S = 55; // constant screen speed, independent of zoom
function _stepFlowParticles(dtS) {
  if (!flowParticles || !frontField) return;
  const b = map.getBounds();
  const west = b.getWest(), east = b.getEast(), south = b.getSouth(), north = b.getNorth();
  const n = flowParticles.x.length;
  for (let i = 0; i < n; i++) {
    flowParticles.age[i] += dtS;
    if (flowParticles.age[i] >= flowParticles.life[i]) {
      _respawnParticle(i, west, east, south, north);
      continue;
    }
    const ll = map.unproject([flowParticles.x[i], flowParticles.y[i]]);
    const s = _sampleFrontField(ll.lng, ll.lat);
    if (!s || s.t_arr_min === null || s.t_arr_min > flowLastFrameT) {
      _respawnParticle(i, west, east, south, north);
      continue;
    }
    // u is east, v is north; screen y grows downward, so v flips sign.
    const mag = Math.hypot(s.u, s.v) || 1;
    const dx = (s.u / mag) * FLOW_SPEED_PX_S * dtS;
    const dy = -(s.v / mag) * FLOW_SPEED_PX_S * dtS;
    flowParticles.x[i] += dx;
    flowParticles.y[i] += dy;
  }
}

function _drawFlowParticles() {
  if (!flowCtx || !flowCanvas || !flowParticles) return;
  const w = window.innerWidth, h = window.innerHeight;
  // Fade previous trails rather than clearing outright -- cheap motion blur.
  flowCtx.globalCompositeOperation = "destination-out";
  flowCtx.fillStyle = "rgba(0,0,0,0.12)";
  flowCtx.fillRect(0, 0, w, h);
  flowCtx.globalCompositeOperation = "source-over";
  flowCtx.fillStyle = "rgba(224, 242, 254, 0.85)";
  const n = flowParticles.x.length;
  for (let i = 0; i < n; i++) {
    const x = flowParticles.x[i], y = flowParticles.y[i];
    if (x < -100 || x > w + 100 || y < -100 || y > h + 100) continue;
    flowCtx.fillRect(x, y, 1.6, 1.6);
  }
}

let _flowLastTs = 0;
function _flowTick(ts) {
  if (document.hidden || !flowFieldOn) { flowRafId = null; return; }
  const dt = _flowLastTs ? Math.min(0.05, (ts - _flowLastTs) / 1000) : 0.016;
  _flowLastTs = ts;
  const start = performance.now();
  _stepFlowParticles(dt);
  _drawFlowParticles();
  // Cost guard: if a tick runs long, skip the next one rather than compound.
  const cost = performance.now() - start;
  flowRafId = requestAnimationFrame((t2) => {
    if (cost > 16) {
      requestAnimationFrame(_flowTick);
    } else {
      _flowTick(t2);
    }
  });
}

function _startFlowLoop() {
  if (flowRafId || document.hidden || !flowFieldOn || !frontField) return;
  _flowLastTs = 0;
  flowRafId = requestAnimationFrame(_flowTick);
}
function _stopFlowLoop() {
  if (flowRafId) cancelAnimationFrame(flowRafId);
  flowRafId = null;
}

// Called from _applySnapshotFrame-adjacent updates so particle spawning
// tracks the same "wet by now" boundary the map itself shows.
function _updateFlowFieldTime(tMin) {
  flowLastFrameT = tMin;
}

window.toggleFlowField = function () {
  flowFieldOn = !flowFieldOn;
  const btn = document.getElementById("btn-toggle-flow");
  if (btn) {
    btn.classList.toggle("is-active", flowFieldOn);
    btn.setAttribute("aria-pressed", flowFieldOn ? "true" : "false");
  }
  if (flowFieldOn) {
    if (frontField) { _seedFlowParticles(); _startFlowLoop(); }
  } else {
    _stopFlowLoop();
    if (flowCtx) flowCtx.clearRect(0, 0, window.innerWidth, window.innerHeight);
  }
};

// ── Evacuation routes (run_layer/evac_routes) ───────────────────────────────
let evacRouteMarkers = [];

// Two layers, not one: `line-dasharray` is a layout-time-only property in
// MapLibre (no data-driven expression support), so "solid while open, dashed
// once lost" needs a STATIC filter split rather than a data expression --
// the same pattern `roads-bridge-cut` already uses for the same reason.
function _addEvacRoutesLayer() {
  map.addSource("evac-routes", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: "evac-routes-line",
    type: "line",
    source: "evac-routes",
    filter: ["!=", ["get", "route_status"], "lost"],
    layout: { "line-join": "round", "line-cap": "round" },
    paint: {
      "line-color": ["match", ["get", "route_status"], "amber", "#f59e0b", "#10b981"],
      "line-width": 3,
      "line-opacity": 0.9,
    },
  });
  map.addLayer({
    id: "evac-routes-line-lost",
    type: "line",
    source: "evac-routes",
    filter: ["==", ["get", "route_status"], "lost"],
    layout: { "line-join": "round", "line-cap": "round" },
    paint: {
      "line-color": "#64748b",
      "line-width": 3,
      "line-dasharray": [2, 1.5],
      "line-opacity": 0.85,
    },
  });

  evacRoutePopup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, maxWidth: "260px" });
  for (const id of ["evac-routes-line", "evac-routes-line-lost"]) {
    map.on("mouseenter", id, (e) => {
      map.getCanvas().style.cursor = "pointer";
      const p = (e.features[0] && e.features[0].properties) || {};
      evacRoutePopup.setLngLat(e.lngLat).setHTML(_evacRoutePopupHtml(p)).addTo(map);
    });
    map.on("mouseleave", id, () => {
      map.getCanvas().style.cursor = "";
      evacRoutePopup.remove();
    });
  }
}

function _evacRoutePopupHtml(p) {
  if (p.length_km !== undefined && +p.length_km === 0) {
    return `<div class="village-popup"><div class="pop-name" dir="auto">${escapeHtml(p.village_name || "Village")}</div>
      <p style="margin:4px 0 0;font-size:11px;color:var(--text-secondary);">
      ${escapeHtml(p.status_note || "Nearest road stays dry — move to high ground nearby.")}</p></div>`;
  }
  const cutTxt = isMissing(p.route_cut_min) ? "not cut in this run" : `cut at T+${Math.round(+p.route_cut_min)} min`;
  return `<div class="village-popup"><div class="pop-name" dir="auto">${escapeHtml(p.village_name || "Village")}</div>
    <dl class="pop-grid" style="margin-top:6px;">
      <dt>Length</dt><dd>${isMissing(p.length_km) ? "—" : (+p.length_km).toFixed(2) + " km"}</dd>
      <dt>Travel</dt><dd>${isMissing(p.travel_min) ? "—" : Math.round(+p.travel_min) + " min"}</dd>
      <dt>Route</dt><dd>${escapeHtml(cutTxt)}</dd>
      <dt>Water there</dt><dd>${isMissing(p.water_arrival_min) ? "—" : "T+" + Math.round(+p.water_arrival_min) + " min"}</dd>
    </dl></div>`;
}

async function _loadEvacRoutes(jobId) {
  evacRoutesGeoJSON = null;
  try {
    const r = await fetch(`/api/run_layer/${jobId}/evac_routes`);
    if (!r.ok) return;
    evacRoutesGeoJSON = await r.json();
  } catch (e) {
    console.warn("evac_routes not available:", e);
  }
}

function _clearEvacRouteMarkers() {
  for (const m of evacRouteMarkers) m.remove();
  evacRouteMarkers = [];
}

// Colours a route by status at time t: green (safe margin), amber (within
// the last 60 min before its cut), red dashed (route lost). Zero-length
// routes (village's nearest road node never gets wet) draw no line -- the
// list/popover note carries the "move to high ground" guidance instead.
let _lastEvacT = null;
function _updateEvacRoutesAtTime(tMin) {
  if (!map || !map.getSource("evac-routes")) return;
  if (!evacRoutesGeoJSON || !evacRoutesGeoJSON.features) return;
  const t = Math.round(tMin);
  if (t === _lastEvacT) return;
  _lastEvacT = t;

  const ranked = [...evacRoutesGeoJSON.features]
    .filter((f) => f.properties && +f.properties.length_km > 0)
    .sort((a, b) => (a.properties.priority_rank || 999) - (b.properties.priority_rank || 999));

  const wanted = selectedEvacVillageId
    ? ranked.filter((f) => f.properties.village_id === selectedEvacVillageId)
    : ranked.slice(0, 5);

  const out = wanted.map((f) => {
    const p = f.properties;
    const cut = isMissing(p.route_cut_min) ? null : +p.route_cut_min;
    let status = "safe";
    if (cut !== null) {
      if (tMin >= cut) status = "lost";
      else if (cut - tMin <= 60) status = "amber";
    }
    return { ...f, properties: { ...p, route_status: status } };
  });
  map.getSource("evac-routes").setData({ type: "FeatureCollection", features: out });

  // Destination "safe ground" dots, pooled like the road-cut markers.
  for (let i = 0; i < out.length; i++) {
    const p = out[i].properties;
    if (isMissing(p.dest_lon) || isMissing(p.dest_lat)) continue;
    let m = evacRouteMarkers[i];
    const cls = `evac-route-arrow${p.route_status === "amber" ? " is-amber" : ""}${p.route_status === "lost" ? " is-lost" : ""}`;
    if (!m) {
      const el = document.createElement("div");
      el.className = cls;
      m = new maplibregl.Marker({ element: el, anchor: "center" })
        .setLngLat([p.dest_lon, p.dest_lat])
        .addTo(map);
      evacRouteMarkers[i] = m;
    } else {
      m.getElement().className = cls;
      m.setLngLat([p.dest_lon, p.dest_lat]);
    }
    m.getElement().title = `Safe ground for ${p.village_name || "village"}`;
    m.getElement().style.display = "";
  }
  for (let i = out.length; i < evacRouteMarkers.length; i++) {
    if (evacRouteMarkers[i]) evacRouteMarkers[i].getElement().style.display = "none";
  }
}

// Called from the "Evacuate first" list (ui.js row hover/click) to show one
// village's route regardless of its rank.
window.showEvacRouteFor = function (villageId) {
  selectedEvacVillageId = villageId || null;
  _lastEvacT = null;
  _updateEvacRoutesAtTime(snapshotFrames.length ? snapshotFrames[currentStep].t_min : 0);
};

// ── Village depth fill (run_layer/village_depth) ────────────────────────────
let villageDepthSeries = null; // {village_id: {name, series: [[t_min, depth_m], ...]}}
const _arrivalRingPlayed = new Set();

async function _loadVillageDepth(jobId) {
  villageDepthSeries = null;
  try {
    const r = await fetch(`/api/run_layer/${jobId}/village_depth`);
    if (!r.ok) return;
    villageDepthSeries = await r.json();
  } catch (e) {
    console.warn("village_depth not available:", e);
  }
}

// Linear interpolation between the two bracketing samples. Returns null
// before the series starts or when the village has no series at all.
function _depthAt(series, tMin) {
  if (!series || !series.length) return null;
  if (tMin <= series[0][0]) return tMin === series[0][0] ? series[0][1] : null;
  for (let i = 1; i < series.length; i++) {
    const [t0, d0] = series[i - 1], [t1, d1] = series[i];
    if (tMin <= t1) {
      if (t1 === t0) return d1;
      const f = (tMin - t0) / (t1 - t0);
      return d0 + (d1 - d0) * f;
    }
  }
  return series[series.length - 1][1];
}

function _updateVillageDepthFill(tMin) {
  if (!villageDepthSeries) return;
  const ramp = depthRamp();
  for (const m of villageMarkers) {
    const el = m.getElement();
    const vid = el && el.dataset && el.dataset.villageId;
    if (!vid) continue;
    const rec = villageDepthSeries[vid];
    const depth = rec ? _depthAt(rec.series, tMin) : null;
    const dot = el.querySelector(".village-dot");
    if (dot) {
      if (depth === null || depth <= 0) {
        dot.style.background = "";
      } else {
        const idx = Math.max(0, Math.min(ramp.length - 1, Math.floor(depth) - 1));
        dot.style.background = ramp[idx];
      }
    }
    if (depth !== null && depth >= 0.3 && !_arrivalRingPlayed.has(vid)) {
      _arrivalRingPlayed.add(vid);
      const ring = el.querySelector(".arrival-ring");
      if (ring) {
        ring.classList.remove("is-playing");
        void ring.offsetWidth;
        ring.classList.add("is-playing");
      }
    }
  }
}

// ── Village marker label collision ──────────────────────────────────────────
// Markers are placed in DOM order (map.js keeps them sorted by priority_rank
// ascending already). After layout, hide the badge of any lower-priority
// marker whose screen box overlaps one already kept -- rank #1 is never
// hidden. Hidden markers keep their dot so the place stays marked.
let _declutterRaf = null;
function _declutterVillageMarkers() {
  if (_declutterRaf) return;
  _declutterRaf = requestAnimationFrame(() => {
    _declutterRaf = null;
    if (!villageMarkers.length) return;
    const boxes = [];
    for (const m of villageMarkers) {
      const el = m.getElement();
      const badge = el.querySelector(".village-badge");
      el.classList.remove("is-collision-hidden");
      if (!badge) { boxes.push(null); continue; }
      const r = badge.getBoundingClientRect();
      boxes.push(r.width ? r : null);
    }
    const kept = [];
    for (let i = 0; i < villageMarkers.length; i++) {
      const el = villageMarkers[i].getElement();
      const isRank1 = el.classList.contains("is-rank1");
      const box = boxes[i];
      if (!box) continue;
      if (isRank1) { kept.push(box); continue; }
      const overlaps = kept.some((k) =>
        box.left < k.right && box.right > k.left && box.top < k.bottom && box.bottom > k.top);
      if (overlaps) {
        el.classList.add("is-collision-hidden");
      } else {
        kept.push(box);
      }
    }
  });
}

function _updateStoryButton() {
  const btn = document.getElementById("btn-story");
  if (btn) btn.disabled = !snapshotFrames.length;
}

// ── Story mode ───────────────────────────────────────────────────────────
// A guided, cancellable fly-through. Any user wheel/drag/click on the map or
// timeline cancels it -- the map stays exactly where that interaction left it.
let _storyCancelled = false;
function _cancelStory() {
  _storyCancelled = true;
  storyRunning = false;
}
function _armStoryCancelListeners() {
  const cancel = () => _cancelStory();
  map.once("wheel", cancel);
  map.once("dragstart", cancel);
  map.once("click", cancel);
  const slider = document.getElementById("time-slider");
  if (slider) slider.addEventListener("pointerdown", cancel, { once: true });
}

const _EASE_CUBIC = (t) => 1 - Math.pow(1 - t, 3);

function _flyToAsync(opts) {
  return new Promise((resolve) => {
    map.once("moveend", resolve);
    map.flyTo({ ...opts, duration: 1400, easing: _EASE_CUBIC });
  });
}
function _wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

window.playStoryMode = async function () {
  if (storyRunning || !snapshotFrames.length) return;
  storyRunning = true;
  _storyCancelled = false;
  _armStoryCancelListeners();

  const dam = _getDamData(document.getElementById("scenario-select")?.value || "annamayya")
    || _getDamData("annamayya");
  const steps = [
    { center: [dam.lon, dam.lat], zoom: 13.5, t: -120 },
    { center: [dam.lon, dam.lat], zoom: 14.2, t: -45 },
    { center: [dam.lon, dam.lat], zoom: 14.5, t: 0 },
    { center: [dam.lon, dam.lat], zoom: 12.5, t: 180 },
    { center: [79.1200, 14.2580], zoom: 12.8, t: 360 },
    { center: [dam.lon, dam.lat], zoom: 10.5, t: 1440 },
  ];

  for (const step of steps) {
    if (_storyCancelled) break;
    await _flyToAsync({ center: step.center, zoom: step.zoom, pitch: 40, bearing: -10 });
    if (_storyCancelled) break;
    if (window.seekToMinute) window.seekToMinute(step.t);
    if (_storyCancelled) break;
    await _wait(2500);
  }
  storyRunning = false;
};

