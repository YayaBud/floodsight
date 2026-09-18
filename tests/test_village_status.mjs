/**
 * The village badge must not call a flooded settlement SAFE.
 *
 *   node tests/test_village_status.mjs
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * `map.js` decided the badge with:
 *
 *     const isAtRisk = isIsolated || (!isMissing(waterMin) && +waterMin < 120);
 *
 * An arbitrary two-hour cutoff, with `max_depth_m` never consulted at all. Any
 * village the flood reached at T+120m or later fell through to the `else` and
 * was labelled SAFE. Measured on `annamayya_stage2_wide`: SIX settlements under
 * water carried a green SAFE badge, including Paparajupalle under 3.78 m.
 *
 * The clincher is Seshamambapuram, the same village in two runs -- T+118m in one
 * and T+122m in the other, which flipped it from "WATER T+118m" to "SAFE" while
 * it took 2.16 m of water either way.
 *
 * This asserts on the REAL `villageStatus` loaded from `frontend/`, never on a
 * re-statement of its rule. INVARIANTS.md records why: `test_mass_gates.py`
 * restated the formula it was checking and therefore could not detect a broken
 * gate.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "frontend", "village_status.js"), "utf8");
// The file is a browser script, not an ES module: evaluate it and take the global.
const villageStatus = new Function(`${src}; return villageStatus;`)();

let failures = 0;
function check(name, cond, detail) {
  if (cond) { console.log(`  ok    ${name}`); }
  else { console.log(`  FAIL  ${name}${detail ? "  -- " + detail : ""}`); failures++; }
}

console.log("village badge: flooded settlements are never SAFE");

// ── The six that were wrong, verbatim from data/scenarios/annamayya_stage2_wide
//    /results.geojson. Every one carries real depth and a late arrival.
const wetButWasSafe = [
  { village_name: "Paparajupalle",   water_arrival_min: 150, max_depth_m: 3.78, priority_rank: 8,  inundated: true },
  { village_name: "Bagidipalle",     water_arrival_min: 127, max_depth_m: 3.26, priority_rank: 10, inundated: true },
  { village_name: "Rachamapalle",    water_arrival_min: 259, max_depth_m: 2.52, priority_rank: 6,  inundated: true },
  { village_name: "Seshamambapuram", water_arrival_min: 122, max_depth_m: 2.16, priority_rank: 5,  inundated: true },
  { village_name: "Iskapalle",       water_arrival_min: 326, max_depth_m: 2.09, priority_rank: 9,  inundated: true },
  { village_name: "Mandaram",        water_arrival_min: 352, max_depth_m: 0.42, priority_rank: 7,  inundated: true },
];
for (const v of wetButWasSafe) {
  const s = villageStatus(v);
  check(`${v.village_name} (${v.max_depth_m} m at T+${v.water_arrival_min}m) is not SAFE`,
        !/SAFE/i.test(s.statusText), `got "${s.statusText}"`);
  check(`${v.village_name} badge states its depth`,
        s.statusText.includes(String(v.max_depth_m)), `got "${s.statusText}"`);
}

// ── The regression that named the bug: arrival time alone must not flip the
//    verdict. Same village, same water, four minutes apart across two runs.
const at118 = villageStatus({ village_name: "Seshamambapuram", water_arrival_min: 118, max_depth_m: 2.16, priority_rank: 5, inundated: true });
const at122 = villageStatus({ village_name: "Seshamambapuram", water_arrival_min: 122, max_depth_m: 2.16, priority_rank: 5, inundated: true });
check("118 vs 122 min does not change the verdict",
      at118.statusClass === at122.statusClass,
      `${at118.statusClass} vs ${at122.statusClass}`);

// ── A never-wet settlement is NOT asserted safe. The front is ~3.3x too slow
//    over a domain that barely drains, so dry means "not reached in this run".
for (const v of [
  { village_name: "Gundlur",  water_arrival_min: null, max_depth_m: 0.00, priority_rank: 16, inundated: false },
  { village_name: "Nandalur", water_arrival_min: null, max_depth_m: 0.04, priority_rank: 12, inundated: false },
]) {
  const s = villageStatus(v);
  check(`${v.village_name} (dry) is not asserted SAFE`,
        !/SAFE/i.test(s.statusText), `got "${s.statusText}"`);
  check(`${v.village_name} (dry) reads NOT REACHED`,
        /NOT REACHED/i.test(s.statusText), `got "${s.statusText}"`);
}

// ── The states that were already right must stay right.
check("rank 1 still evacuates",
      /EVACUATE/.test(villageStatus({ priority_rank: 1, water_arrival_min: 346, max_depth_m: 1.64, inundated: true }).statusText));
check("isolation still outranks a plain flood badge",
      /ISOLATED/.test(villageStatus({ priority_rank: 4, isolation_time_min: 88, water_arrival_min: 97, max_depth_m: 4.24, inundated: true }).statusText));
check("an early arrival still shows its time",
      /T\+97m/.test(villageStatus({ priority_rank: 4, water_arrival_min: 97, max_depth_m: 4.24, inundated: true }).statusText));

// ── An unscored settlement is not a safe one. Absence of a rank is absence of
//    evaluation (INVARIANTS.md), so it gets no badge rather than a green one.
check("an unranked settlement gets no badge",
      villageStatus({ village_name: "Unscored" }).statusText === "",
      `got "${villageStatus({ village_name: "Unscored" }).statusText}"`);

// ── Depth alone is enough, with no arrival time recorded at all.
const noArrival = villageStatus({ village_name: "X", water_arrival_min: null, max_depth_m: 1.20, priority_rank: 9, inundated: true });
check("flooded with no arrival time is still flagged",
      /FLOODED/.test(noArrival.statusText), `got "${noArrival.statusText}"`);

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
