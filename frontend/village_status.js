/**
 * What a settlement's map badge says, and why.
 *
 * Kept in its own file so `tests/test_village_status.mjs` can assert on THIS
 * function rather than on a copy of its rule. INVARIANTS.md records why that
 * matters: `test_mass_gates.py` restated the formula it was checking and so
 * could not detect a broken gate.
 *
 * THE RULE THIS REPLACED
 * ----------------------
 *     const isAtRisk = isIsolated || (!isMissing(waterMin) && +waterMin < 120);
 *
 * An arbitrary two-hour cutoff, with `max_depth_m` never consulted at all.
 * Anything the flood reached at T+120m or later fell through to a green SAFE.
 * Measured on `annamayya_stage2_wide`: six settlements under water carried a
 * SAFE badge, Paparajupalle among them under 3.78 m. Seshamambapuram arrived at
 * T+118m in one run and T+122m in another and flipped from WATER to SAFE on
 * that alone, while taking 2.16 m either way.
 *
 * Danger is DEPTH. Arrival time says when to move, never whether to.
 *
 * WHY THERE IS NO "SAFE"
 * ----------------------
 * A settlement this run never wetted is reported NOT REACHED, not SAFE. The
 * front is ~3.3x too slow against the arrival gate and the domain barely
 * drains, so "no water here in this run" does not support a claim of safety --
 * see INVARIANTS.md, "a settlement reading 0.00 m means 'not reached yet'".
 * NOT REACHED is what the model can actually defend.
 */

// The project's flood threshold: the depth at or above which a cell counts as
// flooded, everywhere else in this codebase (exposure, road cuts, extent).
var VILLAGE_WET_M = 0.3;

function villageStatus(p) {
  p = p || {};
  var missing = function (v) { return v === null || v === undefined || v === "" || (typeof v === "number" && !isFinite(v)); };
  var num = function (v) { return missing(v) ? null : +v; };

  var rank = num(p.priority_rank);
  var isoMin = num(p.isolation_time_min);
  var waterMin = num(p.water_arrival_min);
  var depth = num(p.max_depth_m);

  // `inundated` is the pipeline's own verdict; depth is the fallback when the
  // flag is absent. Either one being true means there is water on this place.
  var flooded = (p.inundated === true) || (depth !== null && depth >= VILLAGE_WET_M);
  var atRisk = (isoMin !== null) || flooded || (waterMin !== null);

  var depthText = (depth !== null) ? depth.toFixed(2) + " m" : null;

  // No rank means this settlement was never scored, and absence of evaluation
  // must not render as an outcome. It gets no badge at all.
  if (rank === null) {
    return { statusClass: "is-normal", statusText: "", atRisk: atRisk };
  }
  if (rank === 1) {
    return { statusClass: "is-rank1", statusText: "RANK #1 · EVACUATE", atRisk: true };
  }
  if (isoMin !== null) {
    return { statusClass: "is-isolated", statusText: "ISOLATED T+" + Math.round(isoMin) + "m", atRisk: true };
  }
  if (flooded) {
    // Both numbers, because they answer different questions: how bad, and how
    // long until it is. The old badge carried only the second.
    return {
      statusClass: "is-risk",
      statusText: (waterMin !== null)
        ? "WATER T+" + Math.round(waterMin) + "m · " + depthText
        : "FLOODED " + depthText,
      atRisk: true,
    };
  }
  if (waterMin !== null) {
    // Water arrives but stays under the flood threshold. Still not "safe".
    return { statusClass: "is-risk", statusText: "WATER T+" + Math.round(waterMin) + "m", atRisk: true };
  }
  return { statusClass: "is-dry", statusText: "NOT REACHED", atRisk: false };
}
