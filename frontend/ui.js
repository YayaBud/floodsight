/**
 * FloodSight — interface glue.
 *
 * Owns the chrome that is not the map and not the timeline: the legend, the
 * download menu, the event popover, and the answer card.
 *
 * Loaded after map.js, so the map's own top-level bindings are already in
 * scope by the time anything here runs.
 */

"use strict";

(function () {

  const $ = (id) => document.getElementById(id);
  const on = (el, ev, fn) => el && el.addEventListener(ev, fn);

  // ── legend ───────────────────────────────────────────────────────────────
  window.toggleLegend = function () {
    const body = $("legend-body");
    const btn = document.querySelector("#map-legend .card-head-btn");
    if (!body || !btn) return;
    const collapsed = body.classList.toggle("is-collapsed");
    btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
  };

  // The ranking shows the top few and expands in place. A full 33-row list is
  // not a decision aid; the first three are.
  window.toggleAllVillages = function () {
    const list = $("priority-list"), btn = $("btn-all-villages");
    if (!list || !btn) return;
    const open = list.classList.toggle("is-expanded");
    btn.textContent = open ? "Show top 3 only" : "View all locations";
  };

  // ── download menu ────────────────────────────────────────────────────────
  window.toggleDownloads = function () {
    const menu = $("download-menu"), btn = $("btn-download");
    if (!menu || !btn) return;
    const open = menu.hidden;
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  };

  function closeDownloads() {
    const menu = $("download-menu"), btn = $("btn-download");
    if (menu && !menu.hidden) {
      menu.hidden = true;
      if (btn) btn.setAttribute("aria-expanded", "false");
    }
  }

  // ── event popover ────────────────────────────────────────────────────────
  window.closeEventPop = function () {
    const pop = $("event-pop");
    if (pop) pop.hidden = true;
    document.querySelectorAll(".pin.is-open").forEach((n) => n.classList.remove("is-open"));
  };

  // Anchored above the pin that opened it. The pin lives in the spine and the
  // popover in the stage, so both are measured against the stage's own box.
  window.positionEventPop = function (anchorEl) {
    const pop = $("event-pop"), stage = $("workspace");
    if (!pop || !stage) return;
    pop.style.bottom = "calc(var(--spine-h) + var(--panel-gap-v) + 12px)";
    if (!anchorEl) { pop.style.left = "50%"; pop.style.transform = "translateX(-50%)"; return; }
    pop.style.transform = "none";
    const s = stage.getBoundingClientRect();
    const a = anchorEl.getBoundingClientRect();
    const w = pop.offsetWidth || 246;
    let x = a.left + a.width / 2 - s.left - w / 2;

    // The left column runs the full height of the stage, so a popover placed
    // over a pin near T+0 would land on top of the answer card. Keep clear of
    // it when it is on screen; the popover is about the timeline, not a
    // reason to hide the finding.
    const col = document.querySelector("#left-panel");
    let minX = 12;
    if (col && col.offsetParent !== null) {
      const c = col.getBoundingClientRect();
      if (c.width) minX = Math.max(minX, c.right - s.left + 12);
    }
    const maxX = s.width - w - 12;
    x = Math.max(minX, Math.min(x, maxX));
    if (x > maxX) x = maxX;                 // narrow stage: right edge wins
    pop.style.left = x + "px";
  };

  // Called by spine.js when a pin is clicked, and by map.js for a village row.
  window.showEventPop = function (evt, anchorEl) {
    const pop = $("event-pop");
    if (!pop) return;
    const kicker = $("pop-kicker");
    const many = evt.members && evt.members.length > 1
      ? ` · ${evt.members.length} events here` : "";
    if (kicker) kicker.textContent = `T+${Math.round(evt.t)} min${many}`;
    const title = $("callout-village");
    if (title) title.textContent = evt.label || "—";

    const p = evt.payload;
    setRow("callout-arr", p && p.water_arrival_min, " min");
    setRow("callout-iso", p && p.isolation_time_min, " min");
    const popEl = $("callout-pop");
    if (popEl) {
      popEl.textContent = (p && p.pop_at_risk !== null && p.pop_at_risk !== undefined
        && !Number.isNaN(+p.pop_at_risk)) ? (+p.pop_at_risk).toLocaleString() : "—";
    }

    const shelterEl = $("callout-shelter");
    if (shelterEl) {
      const scenarioKey = document.getElementById("scenario-select")?.value || "annamayya";
      const SHELTER_NAMES = {
        annamayya: "AP Model School Pullampeta (Elev. 225m)",
        rishiganga: "Joshimath Cantonment Safe Ridge (Elev. 2,150m)",
        phutkal: "Padum High Plateau Relief Center (Elev. 3,650m)",
        south_lhonak: "Chungthang Upper Ridge Assembly Haven (Elev. 1,850m)",
        derna: "Al-Fatayeh High Plateau Emergency Camp (Elev. 145m)",
        ivanovo: "Biser High Hill Community Center (Elev. 185m)",
        malpasset: "Frejus High Ridge Disaster Base (Elev. 45m)",
      };
      shelterEl.textContent = SHELTER_NAMES[scenarioKey] || "High-Ground Safe Haven";
    }

    renderWindow(p);

    pop.hidden = false;
    window.positionEventPop(anchorEl);
  };

  function setRow(id, v, suffix) {
    const el = $(id);
    if (!el) return;
    el.textContent = (v === null || v === undefined || Number.isNaN(+v))
      ? "—" : `T+${Math.round(+v)}${suffix || ""}`;
  }

  // The evacuation window is two timestamps, and their ORDER is the finding —
  // a signed number cannot say which case you are in. So it is written out.
  // ponytail: reads the two timestamps directly rather than trusting
  // evacuation_window_min, whose sign is flagged as unverified in ui.md.
  function renderWindow(p) {
    const line = $("callout-window-line"), val = $("callout-window");
    if (!line || !val) return;
    if (!p) { line.hidden = true; return; }
    const arr = numOrNull(p.water_arrival_min);
    const iso = numOrNull(p.isolation_time_min);
    line.hidden = false;
    line.classList.remove("is-bad");

    if (arr === null || iso === null) {
      val.textContent = "Not enough timing to judge the window here.";
      return;
    }
    const gap = Math.round(iso - arr);
    if (gap > 0) {
      val.textContent = `The last road stays open ${gap} min after the water arrives — open, but not safe to drive.`;
      line.classList.add("is-bad");
    } else if (gap < 0) {
      val.textContent = `${Math.abs(gap)} min between the last road closing and the water arriving.`;
    } else {
      val.textContent = "The road closes as the water arrives. No margin.";
    }
  }

  function numOrNull(v) {
    return (v === null || v === undefined || Number.isNaN(+v)) ? null : +v;
  }

  // ── answer card ──────────────────────────────────────────────────────────
  // Re-read on every playhead move. Only the two rows below the rule change
  // with time; the lead figure and the building count are whole-event totals.
  let _lastAnswerCardT = -999999;
  let _cachedAtEl = null;
  let _cachedTEl = null;
  let _cachedRoads = null;
  let _cachedOf = null;
  let _cachedIso = null;

  window.refreshAnswerCard = function (tMin) {
    const t = Math.round(tMin || 0);
    if (t === _lastAnswerCardT) return;
    _lastAnswerCardT = t;

    const tPrefix = t < 0 ? `T-${Math.abs(t)}` : (t === 0 ? "T 0" : `T+${t}`);
    const phaseNote = t < 0
      ? ' <em style="font-size:10px;color:var(--accent-sky,#38bdf8);font-style:normal;font-weight:600">(Lake Formation)</em>'
      : (t === 0 ? ' <em style="font-size:10px;color:#ef4444;font-style:normal;font-weight:600">(Breach)</em>' : "");
    
    if (!_cachedAtEl && !_cachedTEl) {
      _cachedAtEl = document.querySelector(".ac-at-t");
      _cachedTEl = $("ac-t-val");
    }
    if (_cachedAtEl) {
      _cachedAtEl.innerHTML = `${tPrefix} min${phaseNote}`;
    } else if (_cachedTEl) {
      _cachedTEl.textContent = String(t);
    }

    if (!window.Spine) return;
    const cut = window.Spine.cutAt(t);
    const total = window.Spine.roadCount();
    if (!_cachedRoads) _cachedRoads = $("val-roads");
    if (!_cachedOf) _cachedOf = $("val-roads-of");
    if (!_cachedIso) _cachedIso = $("val-isolated");
    if (_cachedRoads) _cachedRoads.textContent = total ? cut.toLocaleString() : "—";
    if (_cachedOf) _cachedOf.textContent = total ? ` of ${total.toLocaleString()}` : "";
    if (_cachedIso) _cachedIso.textContent = String(window.Spine.isolatedAt(t));
  };

  window.setAnswerCardReady = function () {
    _lastAnswerCardT = -999999;
    _cachedAtEl = null;
    _cachedTEl = null;
    _cachedRoads = null;
    _cachedOf = null;
    _cachedIso = null;
    const card = $("answer-card");
    if (card) card.dataset.state = "full";
    const evac = $("evac-card");
    if (evac) evac.hidden = false;
    const dl = $("btn-download");
    if (dl) dl.disabled = false;
    const rw = $("btn-rewind");
    if (rw) rw.hidden = false;

    // Enable HADR export buttons across header menu and dedicated export card
    ["btn-shp", "btn-kml", "btn-cap", "btn-tif",
     "btn-shp-card", "btn-kml-card", "btn-cap-card", "btn-tif-card"].forEach(id => {
      const b = $(id);
      if (b) b.disabled = false;
    });
  };

  // ── event description ───────────────────────────────────────────────────
  // Full sentence per scenario for the Event card.
  const EVENT_DESC = {
    rishiganga:   "Rishi Ganga natural lake — Uttarakhand, 7 February 2021",
    phutkal:      "Phutkal river landslide dam — Ladakh, 2015",
    south_lhonak: "South Lhonak GLOF & Chungthang dam — Sikkim, 4 October 2023",
    derna:        "Derna, Libya — Abu Mansour + Al-Bilad dam collapse, 11 September 2023",
    ivanovo:      "Ivanovo dam collapse — Biser, Bulgaria, 6 February 2012 (GFD Observed)",
    malpasset:    "Malpasset arch dam — Reyran Valley, France, 2 December 1959 (Canonical Field Benchmark)",
    annamayya:    "Annamayya dam breach — Cheyyeru River, Andhra Pradesh, 19 November 2021",
  };

  window.syncEventDesc = function () {
    const sel = $("scenario-select");
    if (!sel) return;
    const opt = sel.options[sel.selectedIndex];
    const desc = $("event-desc");
    if (desc) desc.textContent = EVENT_DESC[sel.value] || (opt ? opt.text : "");
  };

  // ── panel collapse & zen mode ──────────────────────────────────────────
  window.togglePanel = function (which) {
    if (which === "left") {
      document.body.classList.toggle("left-collapsed");
    } else if (which === "right") {
      document.body.classList.toggle("right-collapsed");
    }
  };

  window.toggleSpine = function () {
    const spine = $("spine");
    if (!spine) return;
    const isCompact = spine.classList.toggle("is-compact");
    const path = $("spine-chevron-path");
    if (path) {
      path.setAttribute("d", isCompact ? "M3 8l4-4 4 4" : "M3 5l4 4 4-4");
    }
  };

  window.toggleZenMode = function () {
    const isZen = document.body.classList.toggle("zen-mode");
    const btn = $("zen-toggle");
    if (btn) btn.classList.toggle("is-active", isZen);
  };

  // ── global wiring ────────────────────────────────────────────────────────
  document.addEventListener("DOMContentLoaded", () => {
    window.syncEventDesc();
    on($("scenario-select"), "change", window.syncEventDesc);

    // Nothing has run yet, so the timeline has nothing to draw.
    if (window.Spine) window.Spine.reset();
  });

  document.addEventListener("keydown", (e) => {
    if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
    if (e.key === "z" || e.key === "Z") {
      window.toggleZenMode();
      return;
    }
    if (e.key === "Escape") {
      if (document.body.classList.contains("zen-mode")) {
        window.toggleZenMode();
        return;
      }
      const menu = $("download-menu");
      if (menu && !menu.hidden) { closeDownloads(); return; }
      window.closeEventPop();
    }
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest(".menu-wrap")) closeDownloads();
    if (!e.target.closest("#event-pop") && !e.target.closest(".pin")
        && !e.target.closest(".priority-item")) {
      window.closeEventPop();
    }
  });

  window.addEventListener("resize", () => {
    const pop = $("event-pop");
    if (pop && !pop.hidden) {
      window.positionEventPop(document.querySelector(".pin.is-open"));
    }
  });
})();
