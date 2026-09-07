/**
 * FloodSight — debug harness
 * ==========================
 * Loaded BEFORE map.js so it can patch MapLibre before any map is built.
 *
 * Why this exists
 * ---------------
 * MapLibre swallows the identity of a failing listener. A stack that reads
 *
 *     TypeError: t.filter is not a function
 *       at e.Map.o (maplibre-gl.js:46:499619)
 *       at e.Map.fire (maplibre-gl.js:42:11958)
 *       at e.Map.resize (...)
 *
 * tells you a listener threw during a resize, but not *which* listener, and the
 * minified frame names are useless. Worse, an exception escaping mid-render
 * leaves MapLibre's task queue with `_currentlyRunning` still set, so every
 * subsequent frame throws "Attempting to run(), but is already running." — the
 * map freezes and the original cause is buried under the cascade.
 *
 * So this file wraps the event machinery, tags every listener with the source
 * line that registered it, and reports the FIRST failure with that tag. It also
 * traces the style API (addSource/addLayer/setFilter/...) because an addLayer
 * that throws leaves later code referring to layers that silently do not exist.
 *
 * Turn off with ?debug=0 in the URL, or FloodSightDebug.disable().
 */

"use strict";

(function () {
  const params = new URLSearchParams(location.search);
  const ENABLED = params.get("debug") === "1" || (typeof sessionStorage !== "undefined" && sessionStorage.getItem("fs-debug") === "1");

  const log = [];
  const counts = Object.create(null);
  let firstError = null;

  function push(level, tag, msg, extra) {
    const rec = {
      t: +(performance.now() / 1000).toFixed(3),
      level, tag, msg,
      extra: extra === undefined ? null : extra,
    };
    log.push(rec);
    counts[level] = (counts[level] || 0) + 1;
    if (log.length > 2000) log.shift();
    const style = level === "error" ? "color:#dc2626;font-weight:bold"
                : level === "warn"  ? "color:#d97706"
                : "color:#0891b2";
    console.log(`%c[FS:${tag}]%c ${msg}`, style, "", extra ?? "");
    render();
    return rec;
  }

  // ── Global traps ────────────────────────────────────────────────────────
  window.addEventListener("error", (e) => {
    const rec = push("error", "window", e.message, {
      file: e.filename, line: e.lineno, col: e.colno,
      stack: e.error && e.error.stack,
    });
    if (!firstError) firstError = rec;
  });
  window.addEventListener("unhandledrejection", (e) => {
    push("error", "promise", String(e.reason && e.reason.message || e.reason),
         { stack: e.reason && e.reason.stack });
  });

  // ── Where was this listener registered? ─────────────────────────────────
  // new Error().stack, minus the frames inside this file, is the registration
  // site. It is the only way to name an anonymous arrow passed to map.on().
  function callSite(skip) {
    const s = (new Error().stack || "").split("\n").slice(skip + 1);
    const line = s.find(l => l.includes("map.js") || l.includes("charts.js"))
              || s[0] || "unknown";
    return line.trim().replace(/^at\s+/, "");
  }

  function patchMap(maplibregl) {
    const P = maplibregl.Map.prototype;

    // 1. Tag every listener with its registration site.
    const origOn = P.on;
    P.on = function (type, layerIdOrListener, maybeListener) {
      const site = callSite(1);
      const hasLayer = typeof layerIdOrListener === "string";
      const listener = hasLayer ? maybeListener : layerIdOrListener;
      if (typeof listener === "function" && !listener.__fsTag) {
        listener.__fsTag = `${type}${hasLayer ? "@" + layerIdOrListener : ""} <- ${site}`;
      }
      push("info", "on", `listen ${type}${hasLayer ? " on layer " + layerIdOrListener : ""}`, site);
      return origOn.apply(this, arguments);
    };

    // 2. Stop a throwing listener from wedging the render loop.
    //
    //    An earlier version of this patch rewrapped the entries in
    //    this._listeners so it could name the individual listener. That breaks
    //    off(): MapLibre removes listeners by identity, so the original
    //    function would no longer be found and delegates would accumulate.
    //    A debug harness that leaks listeners is worse than no harness, so this
    //    wraps fire() itself instead. Less precise about WHICH listener threw,
    //    but it does not touch MapLibre's internal state, and probe (4) below
    //    names the specific failure that was actually reported.
    const origFire = P.fire;
    P.fire = function (event, properties) {
      try {
        return origFire.apply(this, arguments);
      } catch (err) {
        const type = (event && event.type) || event;
        const rec = push("error", "fire",
          `listener threw during "${type}": ${err.message}`,
          { stack: err.stack, state: (() => { try { return state(); }
                                              catch (e) { return null; } })() });
        if (!firstError) firstError = rec;
        return this;   // swallow: escaping here is what freezes the map
      }
    };

    // 3. Trace the style API. An addLayer that throws is invisible otherwise:
    //    later code just finds the layer missing with no explanation.
    for (const m of ["addSource", "addLayer", "removeLayer", "setFilter",
                     "setPaintProperty", "setLayoutProperty", "setStyle"]) {
      const orig = P[m];
      if (typeof orig !== "function") continue;
      P[m] = function (...args) {
        const id = typeof args[0] === "string" ? args[0]
                 : (args[0] && args[0].id) || "?";
        try {
          const r = orig.apply(this, args);
          if (m === "addLayer" || m === "addSource") push("info", m, id);
          return r;
        } catch (err) {
          const rec = push("error", m, `${id}: ${err.message}`,
                           { args: safe(args), stack: err.stack });
          if (!firstError) firstError = rec;
          throw err;
        }
      };
    }

    // 4. The reported freeze is `t.filter is not a function` inside
    //    _createDelegatedListener -- MapLibre's layer-scoped event machinery.
    //    v5's on() normalises a string layer id to an array, so a plain
    //    map.on(type, "layer-id", fn) should be safe and static reading could
    //    not explain how a non-array gets in. This wraps the exact frame: it
    //    reports what was actually passed, and coerces it to an array so the
    //    exception cannot escape into _render() and wedge the task queue.
    const origDelegate = P._createDelegatedListener;
    if (typeof origDelegate === "function") {
      P._createDelegatedListener = function (type, layers, listener) {
        if (!Array.isArray(layers)) {
          const rec = push("error", "delegate",
            `layer-scoped "${type}" listener got a non-array layers argument ` +
            `(${Object.prototype.toString.call(layers)}) -- this is the source ` +
            `of "t.filter is not a function"`,
            { value: safe(layers), registeredAt: callSite(1) });
          if (!firstError) firstError = rec;
          layers = layers == null ? []
                 : (typeof layers === "string" ? [layers] : Array.from(layers || []));
        }
        return origDelegate.call(this, type, layers, listener);
      };
    }

    push("info", "patch", "MapLibre instrumented");
  }

  function safe(v) {
    try { return JSON.parse(JSON.stringify(v)); } catch (e) { return String(v); }
  }

  // ── Snapshot of what the map actually contains right now ────────────────
  function state() {
    // NOT window.map. map.js declares `let map`, and a top-level `let` does not
    // become a window property -- while <div id="map"> DOES, via named-element
    // globals. So window.map returns the container element and every Map method
    // on it is undefined. The bare identifier resolves the real script-scope
    // variable through the global lexical environment.
    const m = (typeof map !== "undefined") ? map : null;
    if (!m || typeof m.isStyleLoaded !== "function") {
      return { map: m ? "not a MapLibre Map (got " + (m.tagName || typeof m) + ")"
                      : "not constructed" };
    }
    let layers = "style not loaded", sources = "style not loaded";
    try {
      const s = m.getStyle();
      if (s) { layers = s.layers.map(l => l.id); sources = Object.keys(s.sources); }
    } catch (e) { layers = "getStyle threw: " + e.message; }
    return {
      styleLoaded: m.isStyleLoaded(), loaded: m.loaded(),
      visibility: document.visibilityState,
      center: m.getCenter && m.getCenter().toArray().map(n => +n.toFixed(4)),
      zoom: m.getZoom && +m.getZoom().toFixed(2),
      layers, sources,
      errors: counts.error || 0,
    };
  }

  // ── Floating panel ──────────────────────────────────────────────────────
  let panel, body, badge;
  function ensurePanel() {
    if (panel || !ENABLED || !document.body) return;
    panel = document.createElement("div");
    panel.id = "fs-debug";
    panel.innerHTML =
      '<div id="fs-debug-bar">' +
        '<strong>DEBUG</strong> <span id="fs-debug-badge">0 errors</span>' +
        '<button id="fs-debug-state" type="button">state</button>' +
        '<button id="fs-debug-copy" type="button">copy</button>' +
        '<button id="fs-debug-clear" type="button">clear</button>' +
        '<button id="fs-debug-min" type="button">_</button>' +
      "</div><div id='fs-debug-body'></div>";
    document.body.appendChild(panel);
    body = panel.querySelector("#fs-debug-body");
    badge = panel.querySelector("#fs-debug-badge");
    panel.querySelector("#fs-debug-state").onclick = () =>
      push("info", "state", "snapshot", state());
    panel.querySelector("#fs-debug-copy").onclick = () =>
      navigator.clipboard.writeText(JSON.stringify(
        { state: state(), firstError, log }, null, 2));
    panel.querySelector("#fs-debug-clear").onclick = () => {
      log.length = 0; for (const k in counts) delete counts[k]; firstError = null; render();
    };
    panel.querySelector("#fs-debug-min").onclick = () =>
      panel.classList.toggle("fs-min");
  }

  let raf = null;
  function render() {
    if (!ENABLED) return;
    ensurePanel();
    if (!body || raf) return;
    raf = setTimeout(() => {
      raf = null;
      badge.textContent = `${counts.error || 0} errors · ${counts.warn || 0} warn`;
      badge.className = (counts.error ? "bad" : "ok");
      body.innerHTML = log.slice(-160).reverse().map(r =>
        `<div class="fs-row fs-${r.level}"><span class="fs-t">${r.t.toFixed(2)}</span>` +
        `<span class="fs-tag">${r.tag}</span>${escapeHtml(r.msg)}` +
        (r.extra ? `<pre>${escapeHtml(typeof r.extra === "string"
            ? r.extra : JSON.stringify(r.extra, null, 1)).slice(0, 1200)}</pre>` : "") +
        "</div>").join("");
    }, 120);
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  }

  // ── Boot ────────────────────────────────────────────────────────────────
  if (ENABLED && window.maplibregl) patchMap(window.maplibregl);
  else if (ENABLED) push("error", "boot", "maplibregl missing — load debug.js AFTER maplibre-gl.js");

  document.addEventListener("DOMContentLoaded", () => {
    if (!ENABLED) return;
    ensurePanel(); render();
    // Report the map's state once it settles, and again after any resize —
    // resize is one of the paths the reported freeze came in on.
    setTimeout(() => push("info", "state", "post-load snapshot", state()), 3000);
    window.addEventListener("resize", () => push("info", "resize",
      `window ${window.innerWidth}x${window.innerHeight}`));
  });

  window.FloodSightDebug = {
    state, log, counts,
    get firstError() { return firstError; },
    dump: () => JSON.stringify({ state: state(), firstError, log }, null, 2),
    disable: () => { if (panel) panel.style.display = "none"; },
  };
})();
