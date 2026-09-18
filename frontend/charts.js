// ── Theme tokens ──────────────────────────────────────────────────────────
// Read from the stylesheet so the charts follow the Survey Datum palette and
// track the light/dark theme, instead of pinning one hard-coded set of hexes.
function chartVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}
const T = () => ({
  paper:  chartVar("--bg-card", "#ffffff"),
  plot:   chartVar("--bg-card", "#ffffff"),
  grid:   chartVar("--border", "#d3d8d2"),
  text:   chartVar("--text-secondary", "#46534e"),
  ink:    chartVar("--text-primary", "#151a19"),
  muted:  chartVar("--text-muted", "#6f7c77"),
  accent: chartVar("--accent-cyan", "#1f5f68"),
  urgent: chartVar("--accent-rose", "#c4531f"),
  cool:   chartVar("--accent-sky", "#6fa5a3"),
  sans:   "'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
  mono:   "'IBM Plex Mono', 'JetBrains Mono', ui-monospace, monospace",
});

/**
 * FloodSight — Charts.js
 * Plotly.js charts: outflow hydrograph, Ritter validation, exposure over time.
 */

"use strict";

// ── Plotly theme ────────────────────────────────────────────────────────
const PLOT_LAYOUT_BASE = {
  paper_bgcolor: "transparent",
  plot_bgcolor:  T().plot,
  font: { family: T().sans, color: T().text, size: 11 },
  margin: { l: 40, r: 16, t: 16, b: 40 },
  xaxis: { gridcolor: T().grid, linecolor: T().grid, zerolinecolor: T().grid },
  yaxis: { gridcolor: T().grid, linecolor: T().grid, zerolinecolor: T().grid },
  legend: { bgcolor: "transparent", font: { size: 10 } },
  hovermode: "x unified",
  hoverlabel: {
    bgcolor: T().paper, bordercolor: T().grid,
    font: { family: T().mono, size: 11, color: T().ink },
  },
};

const PLOTLY_CONFIG = { displayModeBar: false, responsive: true };

// ── Outflow Hydrograph ────────────────────────────────────────────────────
var renderHydrograph = function (data) {
  const el = document.getElementById("hydrograph-chart");
  if (!el || !data) return;

  const traces = [];

  const arms = [
    { key: "pessimistic", label: "Pessimistic",  color: T().urgent, dash: "dot" },
    { key: "central",     label: "Central (Froehlich 2008)", color: T().accent, dash: "solid" },
    { key: "optimistic",  label: "Optimistic",   color: T().cool, dash: "dash" },
  ];

  // Confidence band (shaded area between pessimistic and optimistic)
  if (data.pessimistic && data.optimistic) {
    const t_all = data.central.t_s || data.pessimistic.t_s;
    traces.push({
      x: [...t_all.map(t => t / 60), ...[...t_all].reverse().map(t => t / 60)],
      y: [...data.pessimistic.Q_m3s, ...[...data.optimistic.Q_m3s].reverse()],
      fill: "toself",
      fillcolor: "rgba(31,95,104,0.08)",
      line: { color: "transparent" },
      name: "Ensemble range",
      showlegend: true,
      hoverinfo: "skip",
    });
  }

  arms.forEach(arm => {
    const d = data[arm.key];
    if (!d) return;
    traces.push({
      x: d.t_s.map(t => t / 60),       // convert to minutes
      y: d.Q_m3s,
      mode: "lines",
      line: { color: arm.color, width: arm.key === "central" ? 2 : 1.5, dash: arm.dash },
      name: arm.label,
    });
  });

  const layout = {
    ...PLOT_LAYOUT_BASE,
    xaxis: { ...PLOT_LAYOUT_BASE.xaxis, title: { text: "Time (min)", font: { size: 10 } } },
    yaxis: { ...PLOT_LAYOUT_BASE.yaxis, title: { text: "Q (m³/s)", font: { size: 10 } } },
  };

  Plotly.newPlot("hydrograph-chart", traces, layout, PLOTLY_CONFIG);
};


// ── Ritter Validation Plot ────────────────────────────────────────────────
// This function ONLY plots real data from the API or a deterministic JS
// analytical computation. It does NOT add synthetic noise to simulate solver
// output. The per-solver mini-charts ("ritter-anuga-chart", "ritter-sph-chart")
// previously generated fake curves using Math.sin(). They are not rendered here.
// Those charts are shown as an informational empty state until a real solver is wired.
async function renderRitterPlot() {
  const el = document.getElementById("ritter-combined-chart");
  if (!el) return;

  // Try to load real Ritter JSON from API (last completed job)
  let ritterData = null;
  if (window.currentJobId) {
    try {
      const r = await fetch(`/api/ritter/${window.currentJobId}`);
      if (r.ok) ritterData = await r.json();
    } catch (e) { /* fallback to analytical-only display */ }
  }

  const g  = 9.81;
  let h1, t_s, x_analytical, h_analytical;
  let h_swe_real = null;   // only populated from real solver output
  let h_sph_real = null;   // only populated from real solver output

  if (ritterData && ritterData.ritter) {
    // Real data from server — the ritter key holds the benchmark result
    const rv = ritterData.ritter;
    h1           = rv.h1 || 10.0;
    t_s          = rv.t_s || 60.0;
    x_analytical = rv.x || [];
    h_analytical = rv.h || [];
    // Real FV SWE numerical result if provided
    if (rv.h_numerical) h_swe_real = rv.h_numerical;
    // Real SPH result if provided in the new sph_ritter key
    if (ritterData.sph_ritter && ritterData.sph_ritter.h_sph) {
      h_sph_real = ritterData.sph_ritter.h_sph;
    }
  } else {
    // No real data yet — show only the Ritter analytical curve
    h1   = 10.0;
    t_s  = 60.0;
    const c0  = Math.sqrt(g * h1);
    x_analytical = Array.from({ length: 300 }, (_, i) => -500 + i * (1000 / 299));
    h_analytical = x_analytical.map(xi => {
      if (xi < -c0 * t_s)     return h1;
      if (xi > 2 * c0 * t_s)  return 0;
      return (1 / (9 * g)) * Math.pow(2 * c0 - xi / t_s, 2);
    });
  }

  const traces = [
    // Ritter analytical (always shown)
    {
      x: x_analytical, y: h_analytical,
      mode: "lines",
      line: { color: T().urgent, width: 2.5 },
      name: "Ritter 1892 (analytical, exact)",
    },
  ];

  const annotations = [
    {
      text: `t = ${t_s} s · h₁ = ${h1} m · Frictionless flat bed`,
      x: 0.5, y: 1.04, xref: "paper", yref: "paper",
      showarrow: false, font: { size: 10, color: T().muted },
    },
  ];

  // Only plot FloodSight FV SWE result if real solver data was returned
  if (h_swe_real && h_swe_real.length === x_analytical.length) {
    traces.push({
      x: x_analytical, y: h_swe_real,
      mode: "lines",
      line: { color: T().accent, width: 1.8 },
      name: "FloodSight 2D SWE (COMPUTED LIVE)",
    });
    const rmse_swe = Math.sqrt(
      h_swe_real.reduce((acc, v, i) => acc + Math.pow(v - h_analytical[i], 2), 0) / h_swe_real.length
    ).toFixed(3);
    annotations.push({
      text: `FloodSight SWE RMSE = ${rmse_swe} m (COMPUTED LIVE)`,
      x: 0.01, y: 0.88, xref: "paper", yref: "paper",
      xanchor: "left", showarrow: false,
      font: { size: 11, color: T().accent, family: T().mono },
      bgcolor: T().paper, borderpad: 4,
    });
  }

  // Only plot SPH result if real solver data was returned
  if (h_sph_real && h_sph_real.length === x_analytical.length) {
    traces.push({
      x: x_analytical, y: h_sph_real,
      mode: "lines",
      line: { color: T().cool, width: 1.8, dash: "dash" },
      name: "FloodSight SWE-SPH (COMPUTED LIVE)",
    });
    let rmse_sph = "?";
    if (ritterData && ritterData.sph_ritter && ritterData.sph_ritter.rmse_sph) {
      rmse_sph = ritterData.sph_ritter.rmse_sph.toFixed(3);
    }
    annotations.push({
      text: `FloodSight SPH RMSE = ${rmse_sph} m (COMPUTED LIVE)`,
      x: 0.01, y: 0.79, xref: "paper", yref: "paper",
      xanchor: "left", showarrow: false,
      font: { size: 11, color: T().cool, family: T().mono },
      bgcolor: T().paper, borderpad: 4,
    });
  }

  const layout = {
    ...PLOT_LAYOUT_BASE,
    margin: { l: 52, r: 20, t: 30, b: 52 },
    xaxis: {
      ...PLOT_LAYOUT_BASE.xaxis,
      title: { text: "Distance from dam face (m)", font: { size: 11 } },
    },
    yaxis: {
      ...PLOT_LAYOUT_BASE.yaxis,
      title: { text: "Water depth h (m)", font: { size: 11 } },
    },
    annotations,
  };

  Plotly.newPlot("ritter-combined-chart", traces, layout, PLOTLY_CONFIG);

  // Per-solver mini-charts: show empty state until a real solver is wired.
  // D1 FIX: removed Math.sin() fake curves. The mini-charts now show a message
  // instead of synthetic data dressed up as solver output.
  ["ritter-anuga-chart", "ritter-sph-chart"].forEach(id => {
    const miniEl = document.getElementById(id);
    if (!miniEl) return;
    const isSph = id.includes("sph");
    const solverName = isSph ? "SWE-SPH" : "SWE-FV (ANUGA)";

    if (isSph && h_sph_real) {
      // Real SPH data available
      Plotly.newPlot(id, [
        { x: x_analytical, y: h_analytical, mode: "lines", line: { color: T().urgent, width: 1.5 }, name: "Ritter" },
        { x: x_analytical, y: h_sph_real, mode: "lines", line: { color: T().cool, width: 1.5 }, name: "SWE-SPH (COMPUTED LIVE)" },
      ], { ...PLOT_LAYOUT_BASE, margin: { l: 36, r: 8, t: 8, b: 32 }, showlegend: false }, PLOTLY_CONFIG);
    } else if (!isSph && h_swe_real) {
      // Real FV SWE data available
      Plotly.newPlot(id, [
        { x: x_analytical, y: h_analytical, mode: "lines", line: { color: T().urgent, width: 1.5 }, name: "Ritter" },
        { x: x_analytical, y: h_swe_real, mode: "lines", line: { color: T().accent, width: 1.5 }, name: "SWE-FV (COMPUTED LIVE)" },
      ], { ...PLOT_LAYOUT_BASE, margin: { l: 36, r: 8, t: 8, b: 32 }, showlegend: false }, PLOTLY_CONFIG);
    } else {
      // Solver not yet wired — show the analytical target only, with a note
      Plotly.newPlot(id, [
        { x: x_analytical, y: h_analytical, mode: "lines", line: { color: T().urgent, width: 1.5 }, name: "Ritter 1892 (target)" },
      ], {
        ...PLOT_LAYOUT_BASE,
        margin: { l: 36, r: 8, t: 8, b: 32 },
        showlegend: false,
        annotations: [{
          text: `${solverName} — run a simulation to see solver output`,
          x: 0.5, y: 0.5, xref: "paper", yref: "paper",
          showarrow: false, font: { size: 10, color: T().muted },
        }],
      }, PLOTLY_CONFIG);
    }
  });
}


// ── Demo hydrograph ───────────────────────────────────────────────────────
// This uses Froehlich 2008 equations with the Phutkal scenario parameters to
// generate a plausible-looking hydrograph shape before any simulation runs.
// It is labelled PROXY_DATA because the Hw and Vw values are the configured
// scenario constants, not computed from this session's DEM fill.
function renderDemoHydrograph() {
  // The dashboard no longer carries a hydrograph panel -- the flow lane of the
  // timeline draws the real Q(t) instead, and only after a run. With no target
  // element, Plotly would throw here on a theme change. Nothing proxy-derived
  // goes on screen any more.
  if (!document.getElementById("hydrograph-chart")) return;

  // Froehlich 2008 for Phutkal-like impoundment.
  // These parameters come from published reporting and the scenario config,
  // not from a live DEM computation — hence labelled PROXY_DATA below.
  const G   = 9.81;
  const Hw  = 58;    // Phutkal blockage height from SCENARIOS config [m]
  const Vw  = 28.5e6; // Phutkal volume from SCENARIOS config [m³]

  // Froehlich central
  const B_avg = 0.27 * 1.4 * Math.pow(Vw, 0.32) * Math.pow(Hw, 0.04);
  const tf_s  = 63.2 * Math.sqrt(Vw / (G * Hw * Hw));
  const Qp    = 0.607 * Math.pow(Vw, 0.295) * Math.pow(Hw, 1.24);

  const t_rise = 0.5 * tf_s;
  const t_fall = 1.5 * tf_s;
  const t_end  = t_rise + t_fall + 0.5 * tf_s;
  const dt     = 60;
  const t_arr  = [];
  const Q_cent = [];
  for (let t = 0; t <= t_end; t += dt) {
    t_arr.push(t);
    if (t <= t_rise)                        Q_cent.push(Qp * (t / t_rise));
    else if (t <= t_rise + t_fall)          Q_cent.push(Qp * (1 - 0.9 * (t - t_rise) / t_fall));
    else                                    Q_cent.push(Qp * 0.1);
  }

  const Q_pess = Q_cent.map(q => q * 1.45);
  const Q_opti = Q_cent.map(q => q * 0.72);

  renderHydrograph({
    pessimistic: { t_s: t_arr, Q_m3s: Q_pess, Q_p: Qp * 1.45 },
    central:     { t_s: t_arr, Q_m3s: Q_cent, Q_p: Qp },
    optimistic:  { t_s: t_arr, Q_m3s: Q_opti, Q_p: Qp * 0.72 },
  });

  // Update breach cards with PROXY_DATA label (parameters are from config,
  // not from the DEM-derived fill of this session)
  const el_qp = document.getElementById("bc-qp");
  const el_tf = document.getElementById("bc-tf");
  if (el_qp) el_qp.innerHTML = `Q<sub>p</sub> = ${Math.round(Qp).toLocaleString()} m³/s`;
  if (el_tf) el_tf.innerHTML = `t<sub>f</sub> = ${(tf_s / 3600).toFixed(1)} h`;

  const bp_qp = document.getElementById("bp-qp");
  if (bp_qp) bp_qp.innerHTML = `Q<sub>p</sub> = ${Math.round(Qp*1.45).toLocaleString()} m³/s`;
  const bo_qp = document.getElementById("bo-qp");
  if (bo_qp) bo_qp.innerHTML = `Q<sub>p</sub> = ${Math.round(Qp*0.72).toLocaleString()} m³/s`;
}


// ── Bootstrap ─────────────────────────────────────────────────────────────
// Hydrograph and breach cards stay empty until a completed live run supplies
// the session's actual solver output. The old boot-time proxy curve looked
// like a completed result and could survive while a new run was loading.

// ── Theme changes ─────────────────────────────────────────────────────────
// Plotly bakes colours into the figure at draw time, so a theme switch leaves
// every chart on the old palette — a white plot area sitting in a dark page.
// Re-issue the last render with freshly read tokens instead.
let _lastHydrograph = null;
const _origRenderHydrograph = typeof renderHydrograph === "function" ? renderHydrograph : null;
if (_origRenderHydrograph) {
  renderHydrograph = function (data) {
    _lastHydrograph = data;
    return _origRenderHydrograph(data);
  };
}

document.addEventListener("floodsight:themechange", () => {
  if (typeof Plotly === "undefined") return;
  if (_lastHydrograph && typeof renderHydrograph === "function") {
    renderHydrograph(_lastHydrograph);
  } else if (typeof renderDemoHydrograph === "function") {
    renderDemoHydrograph();
  }
  if (typeof renderRitterPlot === "function") {
    Promise.resolve(renderRitterPlot()).catch(() => {});
  }
  if (typeof window.renderMalpassetBenchmark === "function") {
    Promise.resolve(window.renderMalpassetBenchmark()).catch(() => {});
  }
  if (typeof window.renderScenarioSolverComparison === "function") {
    Promise.resolve(window.renderScenarioSolverComparison()).catch(() => {});
  }
  // Anything still on screen at least gets the new backgrounds.
  document.querySelectorAll(".js-plotly-plot").forEach((el) => {
    Plotly.relayout(el, {
      paper_bgcolor: T().paper,
      plot_bgcolor: T().plot,
      "font.color": T().text,
      "xaxis.gridcolor": T().grid,
      "yaxis.gridcolor": T().grid,
      "xaxis.linecolor": T().grid,
      "yaxis.linecolor": T().grid,
    }).catch(() => {});
  });
});

window.renderMalpassetBenchmark = async function() {
  const container = document.getElementById("malpasset-hwm-chart");
  if (!container || typeof Plotly === "undefined") return;

  try {
    const res = await fetch("/api/benchmarks/malpasset");
    if (!res.ok) return;
    const data = await res.json();

    const hwm = data.high_water_marks || [];
    const xs = hwm.map(p => p.distance_m / 1000); // km
    const yObs = hwm.map(p => p.measured_wse_m);
    const labels = hwm.map(p => p.point);

    // Observations only. There was a second trace here named "FloodSight 2D SWE
    // Simulation", drawn from a `simulated_wse_m` field in the benchmark file.
    // No code in this project produced those numbers — malpasset fails the
    // geometry gate at every coarsening and has never been run — so the field
    // was removed from the data rather than relabelled, and the trace with it
    // (audit Part VI §58, FS-46). Re-add a simulated series only when it comes
    // from an actual run.
    const traces = [
      {
        x: xs,
        y: yObs,
        mode: "markers",
        marker: { color: "#F43F5E", size: 8, symbol: "circle" },
        name: "Surveyed High-Water Mark (P1–P14)",
        text: labels,
        hovertemplate: "%{text}: %{y:.1f} m WSE at %{x:.2f} km<extra></extra>",
      }
    ];

    const notRun = data.simulation_status && data.simulation_status.has_been_run === false;
    const titleText = notRun
      ? "Water Surface Elevation along Valley Profile — surveyed marks only, no simulation on record"
      : "Water Surface Elevation along Valley Profile";

    const layout = {
      ...PLOT_LAYOUT_BASE,
      title: { text: titleText, font: { size: 12, color: T().text, family: T().sans } },
      xaxis: { title: "Distance from Dam (km)", gridcolor: T().grid, linecolor: T().grid, tickfont: { family: T().mono, size: 10, color: T().muted } },
      yaxis: { title: "Max Elevation (m)", gridcolor: T().grid, linecolor: T().grid, tickfont: { family: T().mono, size: 10, color: T().muted } },
      legend: { orientation: "h", y: -0.32, font: { size: 10, family: T().sans } },
      margin: { l: 45, r: 15, t: 30, b: 45 },
      height: 200,
    };

    Plotly.newPlot(container, traces, layout, { responsive: true, displayModeBar: false });
  } catch (err) {
    console.warn("Failed to render Malpasset benchmark:", err);
  }
};

let _lastSolverCompData = null;

window.renderScenarioSolverComparison = async function(compData) {
  const container = document.getElementById("scenario-solver-comparison-chart");
  if (!container || typeof Plotly === "undefined") return;

  if (compData) {
    _lastSolverCompData = compData;
  } else if (_lastSolverCompData) {
    compData = _lastSolverCompData;
  } else if (window.currentJobId) {
    try {
      const res = await fetch(`/api/solver-comparison/${window.currentJobId}`);
      if (res.ok) {
        compData = await res.json();
        _lastSolverCompData = compData;
      }
    } catch (e) {
      console.warn("Could not fetch solver comparison:", e);
    }
  }

  if (!compData) {
    Plotly.newPlot(container, [], {
      ...PLOT_LAYOUT_BASE,
      height: 260,
      margin: { l: 50, r: 50, t: 30, b: 45 },
      annotations: [{
        text: "Run an Annamayya simulation to view 1D SWE-SPH vs 2D FV SWE thalweg profiles",
        x: 0.5, y: 0.5, xref: "paper", yref: "paper",
        showarrow: false, font: { size: 11, color: T().muted }
      }]
    }, PLOTLY_CONFIG);
    return;
  }

  const xs = compData.stations_km || [];
  const fv = compData.fv_peak_depth_m || [];
  const sph = compData.sph_peak_depth_m || [];
  const bed = compData.bed_elevation_m || [];

  const traces = [
    {
      x: xs,
      y: fv,
      mode: "lines",
      name: "2D Finite-Volume SWE (Roe/HLLC)",
      line: { color: "#38BDF8", width: 2.5 },
      hovertemplate: "FV SWE Depth: %{y:.2f} m at %{x:.2f} km<extra></extra>",
      yaxis: "y1"
    },
    {
      x: xs,
      y: sph,
      mode: "lines",
      name: "1D SWE-SPH (Lagrangian Particles)",
      line: { color: "#10B981", width: 2.2, dash: "dot" },
      hovertemplate: "SWE-SPH Depth: %{y:.2f} m at %{x:.2f} km<extra></extra>",
      yaxis: "y1"
    }
  ];

  if (bed.length === xs.length) {
    traces.push({
      x: xs,
      y: bed,
      mode: "lines",
      name: "Thalweg Bed Elevation (DEM)",
      line: { color: "#64748B", width: 1.2, dash: "dash" },
      hovertemplate: "Bed Elevation: %{y:.1f} m at %{x:.2f} km<extra></extra>",
      yaxis: "y2",
      opacity: 0.6
    });
  }

  const rmseStr = compData.rmse_m != null ? `${compData.rmse_m.toFixed(2)} m` : "N/A";
  const wallStr = compData.sph_wall_time_s != null ? `${compData.sph_wall_time_s.toFixed(2)} s` : "N/A";

  const layout = {
    ...PLOT_LAYOUT_BASE,
    height: 280,
    margin: { l: 55, r: 55, t: 35, b: 50 },
    xaxis: {
      ...PLOT_LAYOUT_BASE.xaxis,
      title: { text: "Distance along Cheyyeru Corridor from Dam Face (km)", font: { size: 11, color: T().muted } },
    },
    yaxis: {
      ...PLOT_LAYOUT_BASE.yaxis,
      title: { text: "Peak Depth h (m)", font: { size: 11, color: "#38BDF8" } },
      rangemode: "tozero",
    },
    yaxis2: {
      title: { text: "Bed Elevation (m)", font: { size: 10, color: "#64748B" } },
      overlaying: "y",
      side: "right",
      showgrid: false,
      tickfont: { size: 9, color: T().muted },
    },
    legend: {
      orientation: "h",
      x: 0,
      y: 1.18,
      font: { size: 10, color: T().text },
      bgcolor: "transparent"
    },
    annotations: [
      {
        text: `SWE-SPH vs FV RMSE = ${rmseStr} · SPH Walltime = ${wallStr}`,
        x: 0.99, y: 1.18, xref: "paper", yref: "paper",
        xanchor: "right", showarrow: false,
        font: { size: 10, color: T().accent, family: T().mono },
        bgcolor: T().paper, borderpad: 3
      }
    ]
  };

  Plotly.newPlot(container, traces, layout, PLOTLY_CONFIG);
};

