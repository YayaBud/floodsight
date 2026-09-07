# FloodSight — UI notes

Frontend lives in `floodsight/frontend/`. Served by the same FastAPI app as the API,
so there is no separate frontend server and no build step.

| file | what it is |
|---|---|
| `index.html` | full page — floating header/panels/spine over a full-bleed map, secondary tabs |
| `styles.css` | all styling, including the theme tokens |
| `spine.js` | the timeline: three lanes, event pins, playhead |
| `ui.js` | chrome glue — run form, legend, download menu, popover, answer card |
| `map.js` | MapLibre setup, all layers, run/poll lifecycle |
| `charts.js` | Plotly — Ritter plots on the Solvers tab only |
| `debug.js` | the on-page debug console |
| `maplibre-gl.*`, `plotly-*.min.js` | vendored, do not edit |

## The layout

Rebuilt 2026-09-04 (again) into floating glass panels over a full-bleed map, on the
user's explicit direction — a hand sketch plus Spline/reactbits/lenis.dev as visual
references. **The map is full-bleed and fixed; the chrome floats on a transparent
overlay grid on top of it.** `#stage`/`#map` are `position: fixed; inset: 0`, a
sibling of everything else, z-index 0 — always full viewport, regardless of what
else is on screen. `#workspace` is a second `position: fixed; inset: 0` layer,
z-index 30, `display: grid` with four named areas and `pointer-events: none` on the
grid container itself so the empty gutters (including the whole centre cell, which no
item ever occupies) click and drag straight through to the map; each of the four
children sets `pointer-events: auto` to opt back in.

```
┌──────────────────────────────────────────────────────────┐
│          ╭──────────────── header ────────────────╮       │  44, centred, narrower than the row
│          ╰────────────────────────────────────────╯       │
│  ╭───────────╮                              ╭──────────╮ │
│  │ EMULATION │            map               │ RESULTS  │ │
│  │ (setup +  │      (full-bleed, fixed,      │ (finding,│ │
│  │  run ctrl)│       terrain + hillshade)    │  ranking,│ │
│  │  336px,   │                              │  event,  │ │
│  │  full     │                              │  legend) │ │
│  │  column   │                              │  same    │ │
│  │  height   │                              │  height  │ │
│  ╰───────────╯                              ╰──────────╯ │
│      ╭──────────────── spine ────────────────╮            │  200, centred, narrower than the row
│      ╰────────────────────────────────────────╯            │
└──────────────────────────────────────────────────────────┘
```

Header and spine are deliberately **not** full-width — `justify-self: center` plus a
`max-width` (920px / 1180px) inside their full-width grid row, floating independently
of the side-panel column edges. That, and the side panels running the *full* column
height rather than a short auto-height box, came from a second, more literal
proportion reference the user supplied after the first pass — read the first pass as
"floating panels over a full-bleed map" and the second as "and here are the actual
relative sizes," not two different designs.

**Left (EMULATION)** — the run-setup form, pulled out of what used to be a modal
sheet: scenario, name, water level, failure mode, resolution, reservoir fraction, the
run button, progress, and the breach-arm disclosure. Always visible now; there is no
more `openSetup`/`closeSetup`/scrim/slide-in — those were deleted along with
`#setup-sheet` and `#scenario-chip`.
**Centre** — the map, with the basemap switcher and Download floating bottom-right,
MapLibre's own Navigation/Scale controls at top-left/bottom-left — genuinely clear
map corners now that header/spine don't reach the side edges.
**Right (RESULTS)** — `#answer-card` (the finding) and `#evac-card` (the ranking)
moved here from the left panel, followed by Event, validation (Derna only), Legend.
Stacked, scrolling, never overlapping.
**Spine** — the timeline. It is the page's x-axis, not a widget:

| lane | height | drawn from |
|---|---|---|
| flow depth | remainder | `hydrograph.central` — a filled shape, not a chart |
| events | 66 px | village arrival and isolation times, breach, first road cut |
| roads | 26 px | per-link `cut_time_min` — how much network is left, over time |

One playhead crosses all three, carrying a `T+N min` pill.

The map is **2.5D at `pitch: 40, bearing: -10`** — the original values. It was
flattened to `0, 0` once and reduced to `30, 0` once, and both were rejected on sight.
Leave them alone.

The events lane needs a fixed row height because its labels are two lines (time above
name). `.spine-lanes-wrap > * { min-height: 0 }` is load-bearing — without it the
lanes overflow their grid row and the roads ribbon draws under the transport controls.

### Floating-panel mechanics and its two traps

Sizing tokens: `--panel-w` (336px, 268px ≤1280px), `--panel-gap` (18px — the grid gap
and the left/right outer inset), `--panel-gap-v` (40px — the top/bottom outer inset,
deliberately bigger than `--panel-gap` so the header and spine read as clearly
floating rather than flush; this was a direct user request, don't collapse the two
tokens back into one). Glass look: `--glass-bg`/`--glass-border`/`--glass-blur`
(20px)/`--glass-shadow`, neutral white/black-based translucency in both themes — kept
deliberately off the `#2F6BFF` accent so the glass tint itself doesn't start competing
with chrome the way a tinted glass would (see the accent rule below).

`#left-panel`/`#right-panel` are `align-self: stretch` (the Grid default) — they fill
the entire row-2 height between the header and spine rows, with `overflow-y: auto` as
the only guard against content overflow. An earlier pass here made them auto-height
and capped, on the reasoning that MapLibre's own controls needed a reserved clear
strip at the bottom of each side column; a second, more literal proportion reference
from the user showed tall side panels instead, which turned out to make that whole
reservation unnecessary — see the header/spine note above: once they stopped
stretching to the side edges, the corners MapLibre's controls live in were clear on
their own.

Two things an implementer got right the first time by reading the code rather than
guessing, worth not re-breaking:

- **`#app-header` lives inside `#workspace` now** (for the same Grid collision-safety
  every other chrome region gets), but the tabs it carries must stay usable from
  every tab page. The old rule `body:has(.page:not(.hidden)) #app-main { display:
  none }` would have hidden the header too, since it's now a descendant. Fixed by
  hiding only the dashboard-only regions (`#stage`, `#left-panel`, `#right-panel`,
  `#spine`, `#event-pop`) and raising `#workspace` to `z-index: 30`, above `.page`'s
  `z-index: 15`, so the floating header renders on top of Solvers/Alerts/About.
- **`#event-pop` needs an explicit `pointer-events: auto`.** `#workspace` sets
  `pointer-events: none` so its empty gutters click through to the map, and
  `pointer-events` inherits — without the override the popover's own close button
  and content would be unclickable.

## Terrain and camera motion

Added 2026-09-04, on request ("add something to the map"). A `raster-dem` source
(`terrain-dem`, AWS's open Terrarium tiles, `encoding: "terrarium"`, no key) feeds
both `map.setTerrain({ exaggeration: 1.2 })` and a `hillshade` layer painted directly
above the basemap raster and below every data layer. Added as the first entry in the
`steps` array inside `map.on("load", ...)`, before india-boundary/rivers/flood/etc.,
so it never needs a `beforeId` — everything added after it in the same array-order
stacks on top automatically. `switchBasemap()`/`_applyBasemapPaint()` only ever touch
the `"osm"` source and `"osm-tiles"` layer, so terrain survives every basemap switch
untouched.

Exaggeration is deliberately modest (1.2). This app's flood-depth ramp legibility
against the terrain matters more than dramatic relief — don't push it higher without
checking a real run's depth polygons still read clearly on top.

`flyTo`/`easeTo` calls take `duration: 1400, easing: t => 1 - Math.pow(1 - t, 3)`
(cubic ease-out) for a consistent camera feel. MapLibre's `easing` option is a plain
JS function of `t`, not a CSS cubic-bezier string — `--ease-smooth` (the CSS token
used for panel/hover transitions) has no direct equivalent here and was translated by
hand.

`NavigationControl` stayed at `"top-left"` — briefly moved to `"bottom-left"` when the
header still spanned full width and covered that corner, moved back once the header
became a narrower centred pill and top-left was clear map again. `ScaleControl` is
`"bottom-left"`; MapLibre stacks multiple controls in the same corner automatically,
no conflict.

## Colour

The accent is `#2F6BFF`, and it appears **only on chrome**: Play, the active tab, the
playhead, focus rings, toggles. It is deliberately brighter and more saturated than
every stop in the depth ramp, which tops out at `#1E3A8A`, so it cannot be read as
water.

The road colours in `styles.css` (`--road-open`, `--road-risk`, `--road-cut`,
`--road-bridge`) are the same values the map paints with. If the ribbon and the map
disagree on a hue the lane becomes unreadable, so they must stay in step.

Token names read by JavaScript — `--bg-card`, `--border`, `--text-primary`,
`--text-secondary`, `--text-muted`, `--accent-cyan`, `--accent-rose`, `--accent-sky`,
`--depth-1..6` — are load-bearing. `map.js` reads them through `cssVar()` for the
depth and priority ramps; `charts.js` reads them for Plotly. Renaming one silently
degrades a ramp to its fallback.

## Place names are bidirectional text

`village_name` is the raw OSM `name` tag and for Derna it mixes Latin and Arabic
(`"AOI01 درنة Derna"`). Without isolation the bidi algorithm reorders the name
*together with* the rank number and the score sitting either side of it, and all three
come out scrambled. Every element that prints a place name carries `dir="auto"`, and
`.p-name`, `.pop-name` and `#callout-village` set `unicode-bidi: isolate`. Do not
remove either half.

## The timeline takes no pointer code

A native `<input type="range">` is laid over the lanes at `opacity: 0`. Dragging,
arrow keys, Home/End, and screen-reader announcement all come from the platform. Event
pins sit above it in z-order and are real buttons. There is deliberately no
mousedown/mousemove handling in `spine.js` — do not add any.

## Hidden tabs break animation, and this app expects hidden tabs

A run can take twelve minutes and the poll loop in `runSimulation` is explicitly built
for tabbing away. In a hidden document:

- `requestAnimationFrame` never fires,
- CSS animations freeze wherever they are,
- MapLibre will not load its style at all.

Everything that draws therefore has a hidden-document path. `Spine.render()` paints
synchronously when `document.hidden`, and repaints on `visibilitychange` after
cancelling the stale frame id. `animateCounter` settles on its real value instead of
freezing on a partial number. `@keyframes pin-in` animates transform only and never
opacity, so a frozen entrance still leaves a visible pin.

**If you add an animation here, it must not be the only thing making an element
visible.** That is the trap; it has already been hit once.

## Things that look broken and are not

**Map renders black.** Two separate causes. Either the canvas was hidden at load, in
which case MapLibre never loaded the style — `visibilitychange` now forces a
`resize()` and `triggerRepaint()` on return. Or the basemap tiles are being throttled:
they come from `tile.openstreetmap.org` and `server.arcgisonline.com` over the network
and nothing caches them. Check `performance.getEntriesByType('resource')` for tile
requests with `transferSize: 0`, and try the Satellite basemap, which uses a different
host.

**`InvalidStateError: The source image could not be decoded` at load.** The depth
raster source is registered with a placeholder image before any run exists. Harmless.

**A basemap button click does nothing during load.** `switchBasemap` returns early on
`!map.isStyleLoaded()`. Pre-existing, no feedback given.

**Old job IDs 404 after a server restart.** `_JOBS` is in-memory; see `memory.md`.

## Known UI defects, not yet fixed

**11 villages cut off while 0 roads are cut.** At T+10 the answer card reads both.
The card is reporting the run faithfully — many villages come back with
`isolation_time_min ≈ 0.1` — so the inconsistency is upstream in the pipeline, not in
the display. Observed 2026-09-04, not investigated.

**The evacuation-window sign is still unverified.** The popover no longer prints the
signed `evacuation_window_min`; it writes the relationship out from the two timestamps
("the last road stays open 10 min after the water arrives"), which is unambiguous
either way. The underlying field is still suspect.

**Only 2 of 8 pins carry labels** on a 120-minute domain. Events bunch into the first
30 minutes and `LABEL_GAP` in `spine.js` requires 10.5% of the width. Unlabelled pins
still carry a `title`.

**The ensemble is not on the timeline.** Event pins are points, not ranges, because
only the central arm has arrival times — see `memory.md`.

## Debug console

Append `?debug=1`. Gives an event log, an error/warning counter, and a `state` button
that dumps a map snapshot (`styleLoaded`, `loaded`, `visibility`, `center`, `zoom`,
layer and source counts). Read that snapshot first for any "the map looks wrong"
report; it separates a rendering problem from a data one without touching the code.

## Verifying a UI change

Drive the real page rather than asking someone to look. Start the server, open it,
then use `read_page` for structure, `read_console_messages` for errors, and a
screenshot only for genuinely visual changes.

**Front the browser tab before measuring anything.** A hidden pane reports
`innerWidth: 0`, which makes every `getBoundingClientRect()` zero and every layout
look collapsed. That wastes a lot of time looking like a CSS bug.

Overlay geometry has automated coverage in `tests/test_overlay_geometry.py` — a change
to how polygons are generated should be checked there first, since it fails on the
numbers rather than on how a screenshot looks.
