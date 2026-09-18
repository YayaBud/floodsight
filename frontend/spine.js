/**
 * FloodSight — the spine.
 *
 * The timeline is the page's x-axis, not a widget. Three lanes share one
 * domain (T+0 .. T+max) and one playhead:
 *
 *   flow    outflow Q(t) as a filled shape — context, not a chart
 *   events  breach, first road cut, arrivals and last-exit times
 *   roads   how much of the road network is still open, minute by minute
 *
 * All of it is drawn from data the run already produces. Nothing here fetches.
 *
 * Input is handled by the native <input type="range"> laid transparently over
 * the lanes, so dragging, keyboard stepping, Home/End and screen-reader
 * announcement all come from the platform rather than from pointer code here.
 */

"use strict";

window.Spine = (function () {

  const FLOW_W = 1000, FLOW_H = 60;      // viewBox of #lane-flow
  const ROAD_W = 1000, ROAD_H = 26;      // viewBox of #lane-roads
  const ROAD_BINS = 140;
  const RISK_LEAD_MIN = 15;              // "cut soon" = cut within this many minutes
  const MIN_PIN_GAP = 0.024;             // fraction of width before pins merge
  const LABEL_GAP   = 0.065;             // fraction of width needed to show a label

  let tMin = 0;                          // domain start, minutes (can be negative for lake formation)
  let tMax = 0;                          // domain end, minutes
  let events = [];                       // [{t, label, kind, major, payload}]
  let placed = [];                       // after clustering
  let _pinEls = [];                      // pre-cached pin buttons
  let _pinPassed = [];                   // boolean state cache per pin
  let roadTimes = null;                  // sorted array of per-link cut times
  let roadTotal = 0;
  let isoTimes = [];                     // sorted village isolation times
  let lastHyd = null;

  const $ = (id) => document.getElementById(id);
  const span = () => Math.max(1e-6, tMax - tMin);
  const pct = (t) => (tMax > tMin ? Math.max(0, Math.min(1, (t - tMin) / span())) : 0);
  const reduced = () =>
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  // Only animate when a frame will actually be delivered and motion is wanted.
  const animatable = () => !document.hidden && !reduced();

  function fmtT(t) {
    const roundT = Math.round(t);
    const simLabel = roundT < 0 ? `T-${Math.abs(roundT)}m` : (roundT === 0 ? `T 0m` : `T+${roundT}m`);
    
    // Check if the current scenario has a historical event clock configured
    try {
      const activeDam = window.CURRENT_DAM;
      if (activeDam && activeDam.event_clock && activeDam.event_clock.origin_iso) {
        const d = new Date(activeDam.event_clock.origin_iso);
        d.setMinutes(d.getMinutes() + roundT);
        let hours = d.getHours();
        const ampm = hours >= 12 ? "PM" : "AM";
        hours = hours % 12 || 12;
        const hh = String(hours).padStart(2, "0");
        const mm = String(d.getMinutes()).padStart(2, "0");
        return `${simLabel} (${hh}:${mm} ${ampm})`;
      }
    } catch (_) {}

    return simLabel;
  }

  // ── domain ───────────────────────────────────────────────────────────────
  function setDomain(frames) {
    if (!frames || !frames.length) return;
    tMin = +frames[0].t_min || 0;
    tMax = +frames[frames.length - 1].t_min || 0;
    const spine = $("spine");
    if (spine) {
      spine.dataset.state = "ready";
      spine.classList.remove("is-compact");
      const path = $("spine-chevron-path");
      if (path) path.setAttribute("d", "M3 5l4 4 4-4");
    }
    const empty = $("spine-empty");
    if (empty) empty.hidden = true;
    const lanes = $("spine-lanes");
    if (lanes) lanes.hidden = false;
    const start = document.querySelector(".spine-ends span:first-child");
    if (start) start.textContent = `${fmtT(tMin)} min`;
    const end = $("scrubber-max");
    if (end) end.textContent = `${fmtT(tMax)} min`;
    render();
  }

  // ── flow lane ────────────────────────────────────────────────────────────
  // The central arm only. The outer arms are envelope-only solves, so they
  // carry no timeline the other lanes could be read against.
  function setFlow(hyd) {
    lastHyd = hyd;
    drawFlow();
  }

  function drawFlow() {
    const area = $("flow-area"), line = $("flow-line");
    if (!area || !line) return;
    const d = lastHyd && lastHyd.central;
    if (!d || !d.t_s || !d.Q_m3s || !d.t_s.length) {
      area.setAttribute("d", ""); line.setAttribute("d", "");
      return;
    }

    const n = d.t_s.length;
    const stride = Math.max(1, Math.floor(n / 260));
    const qMax = d.Q_m3s.reduce((m, q) => (q > m ? q : m), 0) || 1;

    const zeroX = pct(0) * FLOW_W;
    let path = `M${zeroX.toFixed(1)} ${FLOW_H}`;
    for (let i = 0; i < n; i += stride) {
      const x = pct(d.t_s[i] / 60) * FLOW_W;
      const y = FLOW_H - (d.Q_m3s[i] / qMax) * (FLOW_H - 3);
      path += ` L${x.toFixed(1)} ${y.toFixed(1)}`;
    }
    line.setAttribute("d", path);
    area.setAttribute("d", `${path} L${FLOW_W} ${FLOW_H} L${zeroX.toFixed(1)} ${FLOW_H} Z`);
  }

  // ── events lane ──────────────────────────────────────────────────────────
  function setEvents(features, meta) {
    events = [];
    const activeScenario = (meta && (meta.scenario || meta.scenario_key)) ||
      (window.CURRENT_SCENARIO_KEY || document.getElementById("scenario-select")?.value || "");

    if (activeScenario === "annamayya") {
      // Historical evidence disaster sequence (EVD-01 through EVD-18)
      if (tMin <= -170) {
        events.push({
          t: tMin,
          label: "Rainfall Onset (EVD-01, 180mm)",
          kind: "weather",
          major: false,
          payload: {
            village_name: "Catchment Storm Onset",
            water_arrival_min: `${tMin}`,
            description: "Jawad precursor depression delivers 180mm storm over Cheyyeru basin (EVD-01)"
          }
        });
      }
      events.push({
        t: -150,
        label: "Pincha Ring Bund Washout (EVD-04)",
        kind: "cascade",
        major: true,
        payload: {
          village_name: "Pincha Dam",
          water_arrival_min: "-150",
          description: "Upstream temporary ring bund washed out at 03:15 AM IST (T-150 min), releasing 1.40 lakh cusecs surge down Cheyyeru (EVD-04/05)"
        }
      });
      events.push({
        t: -25,
        label: "Surge Arrives at Reservoir (EVD-07)",
        kind: "cascade",
        major: false,
        payload: {
          village_name: "Annamayya Reservoir",
          water_arrival_min: "-25",
          description: "Cheyyeru gorge transit completed (EVD-07); reservoir storage rises rapidly from FRL toward crest"
        }
      });
      events.push({
        t: -15,
        label: "Spillway Overwhelmed (EVD-12/14)",
        kind: "lake",
        major: false,
        payload: {
          village_name: "Annamayya Spillway",
          water_arrival_min: "-15",
          description: "4 operational radial gates discharge at full 4,136 m³/s capacity; gate #4 jammed inoperable (EVD-14)"
        }
      });
      events.push({
        t: 0,
        label: "Overtopping Initiation (EVD-16)",
        kind: "breach",
        major: true,
        payload: {
          village_name: "Annamayya Dam Crest",
          water_arrival_min: "0",
          description: "Water level exceeds +206.0m crest at 05:45 AM IST (T=0), initiating embankment erosion (EVD-16)"
        }
      });
      events.push({
        t: 30,
        label: "Embankment Collapse (EVD-17)",
        kind: "breach",
        major: true,
        payload: {
          village_name: "336m Earthen Section",
          water_arrival_min: "30",
          description: "Full washout of 336m earthen bund at 06:15 AM IST (T+30 min), peak release ~12,200 m³/s (EVD-17/18)"
        }
      });
      events.push({
        t: 45,
        label: "MHA Failure Time (EVD-17)",
        kind: "breach",
        major: false,
        payload: {
          village_name: "Official Incident Record",
          water_arrival_min: "45",
          description: "Disaster timestamp recorded in Ministry of Home Affairs report D692 (06:30 AM IST, EVD-17)"
        }
      });
    } else {
      if (tMin < 0) {
        events.push({
          t: tMin,
          label: "River Blockage & Lake Formation",
          kind: "lake",
          major: true,
          payload: {
            village_name: "Lake Impoundment",
            water_arrival_min: `${tMin}`,
            isolation_time_min: null,
            pop_at_risk: 0,
            description: "Natural barrier / river blockage impounds upstream water"
          }
        });
        events.push({
          t: -10,
          label: "Spillway Capacity Reached",
          kind: "lake",
          major: false,
          payload: {
            village_name: "Dam Crest Level",
            water_arrival_min: "-10",
            isolation_time_min: null,
            pop_at_risk: 0,
            description: "Reservoir reaches 100% capacity; overtopping threshold initiated"
          }
        });
      }
      events.push({ t: 0, label: "Dam Breach", kind: "breach", major: true, payload: null });

      if (meta && meta.cascade_arrival_min) {
        events.push({
          t: meta.cascade_arrival_min,
          label: "Surge Hits Downstream Dam",
          kind: "cascade",
          major: true,
          payload: {
            village_name: meta.downstream_structure?.name || "Downstream Structure",
            water_arrival_min: `${meta.cascade_arrival_min}`,
            isolation_time_min: null,
            pop_at_risk: 0,
            description: `Upstream breach wave reaches downstream structure, initiating cascading failure`
          }
        });
      }
    }

    const ranked = (features || [])
      .filter((f) => f && f.properties)
      .sort((a, b) => (a.properties.priority_rank || 99) - (b.properties.priority_rank || 99));

    isoTimes = [];
    ranked.forEach((f, i) => {
      const p = f.properties;
      const name = p.village_name || "village";
      const arr = num(p.water_arrival_min);
      const iso = num(p.isolation_time_min);
      if (iso !== null) isoTimes.push(iso);
      if (arr !== null) {
        events.push({
          t: arr, label: `Water reaches ${name}`, kind: "arrival",
          major: i === 0, payload: p,
        });
      }
      if (iso !== null) {
        events.push({
          t: iso, label: `Last road out — ${name}`, kind: "isolation",
          major: false, payload: p,
        });
      }
    });

    isoTimes.sort((a, b) => a - b);
    render();
  }

  // How many of a sorted array are <= t. Shared by the roads ribbon and by the
  // answer card, which both need a count at the playhead rather than a total.
  function countUpTo(sorted, t) {
    if (!sorted || !sorted.length) return 0;
    let lo = 0, hi = sorted.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (sorted[m] <= t) lo = m + 1; else hi = m; }
    return lo;
  }

  function num(v) {
    if (v === null || v === undefined || Number.isNaN(+v)) return null;
    return +v;
  }

  // ── roads lane ───────────────────────────────────────────────────────────
  function setRoads(geojson) {
    const feats = (geojson && geojson.features) || [];
    roadTotal = feats.length;
    const times = [];
    for (const f of feats) {
      const t = num(f.properties && f.properties.cut_time_min);
      if (t !== null) times.push(t);
    }
    times.sort((a, b) => a - b);
    roadTimes = times;

    const count = $("roads-count");
    if (count) {
      count.textContent = roadTotal ? `${times.length} of ${roadTotal} cut` : "";
    }
    // The earliest cut is a decision point, so it earns a pin of its own.
    if (times.length) {
      events = events.filter((e) => e.kind !== "firstcut");
      events.push({
        t: times[0], label: "First road cut", kind: "firstcut",
        major: false, payload: null,
      });
    }
    render();
  }

  // Cumulative composition of the network over time. Monotone by construction:
  // a link that is cut stays cut, so `cut` only ever grows.
  function drawRoads() {
    const openEl = $("roads-open-area"), riskEl = $("roads-risk-area"), cutEl = $("roads-cut-area");
    if (!openEl || !riskEl || !cutEl) return;
    if (!roadTimes || !roadTotal || span() <= 0) {
      openEl.setAttribute("d", ""); riskEl.setAttribute("d", ""); cutEl.setAttribute("d", "");
      return;
    }

    const upto = (t) => countUpTo(roadTimes, t);

    const cutTop = [], riskTop = [];
    for (let b = 0; b <= ROAD_BINS; b++) {
      const t = tMin + (b / ROAD_BINS) * span();
      const x = (b / ROAD_BINS) * ROAD_W;
      const nCut  = t < 0 ? 0 : upto(t);
      const nRisk = t < 0 ? 0 : Math.max(0, upto(t + RISK_LEAD_MIN) - nCut);
      const yCut  = ROAD_H - (nCut / roadTotal) * ROAD_H;
      const yRisk = ROAD_H - ((nCut + nRisk) / roadTotal) * ROAD_H;
      cutTop.push([x, yCut]);
      riskTop.push([x, yRisk]);
    }

    const band = (top, bottom) => {
      let d = "M" + top.map((p) => `${p[0].toFixed(1)} ${p[1].toFixed(2)}`).join(" L");
      for (let i = bottom.length - 1; i >= 0; i--) {
        d += ` L${bottom[i][0].toFixed(1)} ${bottom[i][1].toFixed(2)}`;
      }
      return d + " Z";
    };
    const floor = cutTop.map((p) => [p[0], ROAD_H]);
    const ceil  = cutTop.map((p) => [p[0], 0]);

    cutEl.setAttribute("d",  band(cutTop, floor));
    riskEl.setAttribute("d", band(riskTop, cutTop));
    openEl.setAttribute("d", band(ceil, riskTop));
  }

  // ── pin placement ────────────────────────────────────────────────────────
  // Pins closer together than MIN_PIN_GAP merge into one, because two dots a
  // pixel apart is not a readable timeline. The cluster says how many it holds
  // and snaps to the first of them.
  // ponytail: a cluster opens its earliest event rather than a sub-list. If
  // clusters routinely hold unrelated events, give them their own popover list.
  function clusterEvents() {
    const sorted = [...events].filter((e) => e.t <= tMax + 1e-6).sort((a, b) => a.t - b.t);
    const out = [];
    for (const e of sorted) {
      const prev = out[out.length - 1];
      if (prev && pct(e.t) - pct(prev.t) < MIN_PIN_GAP) {
        prev.members.push(e);
        if (e.major) { prev.major = true; prev.label = e.label; prev.payload = e.payload; }
      } else {
        out.push({ ...e, members: [e] });
      }
    }
    return out;
  }

  function drawPins() {
    const host = $("event-pins");
    if (!host) return;
    host.textContent = "";
    placed = clusterEvents();
    _pinEls = [];
    _pinPassed = [];

    // Historical arrival-window bands were removed: the backend deliberately
    // withdrew this exact data (src/m10_validation/compare_arrivals.py sets
    // HISTORICAL_ARRIVALS = {} after ruling it not eligible as historical
    // truth without a source manifest). Real data belongs behind
    // /api/validation/{id}/arrivals when that is wired into the timeline.

    placed.forEach((e, i) => {
      const p = pct(e.t);
      const btn = document.createElement("button");
      btn.type = "button";
      // The entrance animation is opt-in. CSS animations do not run in a hidden
      // tab, and one that fades in from opacity 0 after a delay leaves the pin
      // invisible for good if the run landed in the background.
      btn.className = "pin" + (e.major ? " pin-major" : "") + (animatable() ? " pin-in" : "");
      btn.style.left = (p * 100).toFixed(3) + "%";
      if (animatable()) btn.style.animationDelay = `${Math.min(i * 45, 400)}ms`;
      const many = e.members.length > 1 ? ` (+${e.members.length - 1} more)` : "";
      btn.title = `${fmtT(e.t)} min · ${e.label}${many}`;
      btn.setAttribute("aria-label", btn.title);
      btn.dataset.idx = String(i);
      btn.addEventListener("click", () => openPin(i));
      host.appendChild(btn);
      _pinEls.push(btn);
      _pinPassed.push(false);

      // Labels only where there is room; otherwise the pin's title carries it.
      const prev = placed[i - 1], next = placed[i + 1];
      const room =
        (!prev || p - pct(prev.t) > LABEL_GAP) &&
        (!next || pct(next.t) - p > LABEL_GAP);
      if (!room && !e.major) return;

      // Time above, name below, both centred on the pin. Stacking them means a
      // long place name cannot drag the time out of alignment with its dot.
      const lab = document.createElement("span");
      const isStaggered = i % 2 === 1;
      lab.className = "pin-label" + (isStaggered ? " pin-label-stagger" : "") + (animatable() ? " pin-in" : "");
      lab.style.left = (p * 100).toFixed(3) + "%";
      if (isStaggered) lab.style.top = "24px";
      if (animatable()) lab.style.animationDelay = `${Math.min(i * 45 + 60, 460)}ms`;
      lab.innerHTML =
        `<b>${fmtT(e.t)} min</b><span dir="auto">${escapeHtmlLocal(e.label)}</span>`;
      host.appendChild(lab);
    });
  }

  function escapeHtmlLocal(x) {
    return String(x).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // Snap the playhead to the pin, then show what it is.
  function openPin(i) {
    const e = placed[i];
    if (!e) return;
    if (window.seekToMinute) window.seekToMinute(e.t);
    for (let j = 0; j < _pinEls.length; j++) {
      _pinEls[j].classList.remove("is-open");
    }
    const node = _pinEls[i];
    if (node) node.classList.add("is-open");
    if (window.showEventPop) window.showEventPop(e, node);
  }

  // ── playhead ─────────────────────────────────────────────────────────────
  function setTime(tVal) {
    const p = pct(tVal);
    const head = $("playhead");
    if (head && span() > 0) {
      head.style.transform = `translateX(${(p * 100).toFixed(4)}%)`;
      const tag = $("playhead-tag");
      if (tag) tag.textContent = `${fmtT(tVal)} min`;
    }
    // Read-only twin of the slider in the footer. It shows position without
    // being a second control for the same value.
    const fill = $("ends-fill");
    if (fill) fill.style.width = (p * 100).toFixed(3) + "%";

    const eps = tVal + 1e-6;
    for (let i = 0; i < _pinEls.length; i++) {
      const e = placed[i];
      const isPassed = !!e && e.t <= eps;
      if (_pinPassed[i] !== isPassed) {
        _pinEls[i].classList.toggle("pin-passed", isPassed);
        _pinPassed[i] = isPassed;
      }
    }
  }

  function setPlaying(on) {
    const spine = $("spine");
    if (spine) spine.dataset.playing = on && !reduced() ? "true" : "false";
  }

  // ── render ───────────────────────────────────────────────────────────────
  let raf = 0;
  function paint() {
    raf = 0;
    if (tMax <= 0) return;
    drawFlow();
    drawRoads();
    drawPins();
  }

  // A run can finish while the tab is in the background -- the poll loop is
  // built for exactly that -- and requestAnimationFrame does not fire in a
  // hidden document. Coalescing into a frame that never arrives left the
  // timeline blank until something else happened to redraw it, so a hidden
  // document paints straight away and visibilitychange repaints on return.
  function render() {
    if (document.hidden) { paint(); return; }
    if (raf) return;
    raf = requestAnimationFrame(paint);
  }

  // Counts at the playhead, for the answer card. These two are genuinely
  // time-varying because the pipeline emits per-link cut times and per-village
  // isolation times. Population and buildings are whole-event totals and are
  // deliberately not faked into time series here.
  function cutAt(t)      { return countUpTo(roadTimes, t); }
  function isolatedAt(t) { return countUpTo(isoTimes, t); }
  function roadCount()   { return roadTotal; }

  function reset() {
    tMax = 0; events = []; placed = []; roadTimes = null; roadTotal = 0;
    isoTimes = []; lastHyd = null;
    const spine = $("spine");
    if (spine) { spine.dataset.state = "empty"; spine.dataset.playing = "false"; }
    const empty = $("spine-empty"); if (empty) empty.hidden = false;
    const lanes = $("spine-lanes"); if (lanes) lanes.hidden = true;
    const host = $("event-pins"); if (host) host.textContent = "";
    const count = $("roads-count"); if (count) count.textContent = "";
  }

  window.addEventListener("resize", render);
  document.addEventListener("floodsight:themechange", render);
  // A frame requested just before the tab was hidden never arrives, and the
  // pending id would make render() think a repaint was already queued. Drop it
  // and paint for real on the way back in.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    render();
  });

  return { setDomain, setFlow, setEvents, setRoads, setTime, setPlaying, reset,
           cutAt, isolatedAt, roadCount,
           get tMax() { return tMax; } };
})();
