"""Can the Cheyyeru, as this model represents it, move water fast enough?

Terrain + Manning only. No solver, no run directory, ~20 s.

Background
----------
`findings_results.md` (2026-09-14) measured every structural candidate for the
Annamayya front being ~3.3x too slow and closed all of them:

    flowline + outlets + channel roughness   17 %
    + coarsen 3 -> coarsen 2                 33 % cumulative
    antecedent wet channel                   41 % on one point, contaminated

and ended "Do not spend another session on these four." What it did NOT do was
ask the prior question: **on this reach's actual bed slope, what velocity does
uniform flow permit at all?** If Manning normal flow at the sourced discharge is
already below the celerity the arrival gate demands, then no roughness, outlet
or resolution choice can ever reach it, and the deficit is a BOUND to be
reported rather than a defect to be chased.

Method
------
1.  Build the grid the routed run uses (`route_annamayya.build_grid`), which
    coarsens by block MINIMUM so a narrow channel is not averaged away with its
    banks.
2.  Take the OSM river stem and clip it to the FLOOD REACH -- the first station
    at or below the dam crest, down to the downstream end. Sampling the whole
    stem instead is the easy mistake: it includes mountain headwaters and
    reports S ~ 1.3e-2, an order of magnitude steeper than the reach the flood
    actually travels.
3.  Use the monotone-downstream envelope of the bed for the slope, not the raw
    profile. A 30 m surface model invents adverse rises along a valley floor;
    the envelope is the slope actually available to the flow.
4.  Manning normal depth and velocity for a wide rectangular channel:

        h = (Q n / (W sqrt(S)))^(3/5)      V = Q / (W h)

    reported at one cell width and at a declared true channel width, because a
    50-100 m channel is sub-grid at 61-152 m cells and the two give different
    answers.

What the numbers mean is in `implementation_plan.md` item 4 and in
`findings_results.md` under 2026-09-18.

Usage
-----
    python scripts/diagnostics/check_conveyance.py
    python scripts/diagnostics/check_conveyance.py --check     # self-check
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import rasterio

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.route_annamayya import build_grid          # noqa: E402
from src.data_fetcher import build_rivers                # noqa: E402
from src.m2_geometry.dem_utils import condition_flowline  # noqa: E402

# The dam crest, from the annamayya cascade reservoir spec. Stations above this
# are upstream of the structure and are not part of the flood reach.
DAM_CREST_M = 206.0

# Celerity the arrival gate demands: the record's measured 1.38 m/s at coarsen 5
# multiplied by the 3.3x the gate is short by.
MEASURED_CELERITY_MS = 1.38
REQUIRED_CELERITY_MS = 4.5

# Discharges worth asking the question at, and where each one comes from.
DISCHARGES = (
    (800.0, "sourced baseflow"),
    (1309.0, "model's measured peak, coarsen 5"),
    (3964.0, "official surge estimate"),
)

# A 50-100 m channel is sub-grid at these cell sizes; 80 m is the midpoint of
# the range the record states. It is an ASSUMPTION, reported as one.
TRUE_CHANNEL_W_M = 80.0
MANNING_N = 0.035


def manning_normal(Q: float, W: float, S: float, n: float = MANNING_N):
    """Normal depth and velocity for a wide rectangular channel."""
    if S <= 0:
        return float("nan"), float("nan")
    h = (Q * n / (W * np.sqrt(S))) ** 0.6
    return h, Q / (W * h)


def reach_profile(z_grid, transform, line, step_m):
    """Bed elevation along `line`, clipped to the reach at or below the crest."""
    n = max(2, int(line.length // step_m))
    pts = [line.interpolate(i / n, normalized=True) for i in range(n + 1)]
    xs = np.array([p.x for p in pts])
    ys = np.array([p.y for p in pts])
    rows, cols = rasterio.transform.rowcol(transform, xs, ys)
    rows = np.clip(np.asarray(rows), 0, z_grid.shape[0] - 1)
    cols = np.clip(np.asarray(cols), 0, z_grid.shape[1] - 1)
    z_all = z_grid[rows, cols]
    s_all = np.arange(len(pts)) * (line.length / n)

    below = np.where(z_all <= DAM_CREST_M)[0]
    if not len(below):
        return None, None
    i0 = int(below[0])
    return z_all[i0:], s_all[i0:] - s_all[i0]


def analyse(coarsen: int, verbose: bool = True) -> dict:
    dem, tr, crs, dx, dy, _ = build_grid(coarsen)
    geoms = list(build_rivers("annamayya").to_crs(crs).geometry)
    dem_c, _channel, rep = condition_flowline(dem, tr, geoms)
    stem = max(geoms, key=lambda g: g.length)

    out = {"coarsen": coarsen, "cell_m": float(dx),
           "flowline_conditioned": bool(rep.get("available"))}

    for label, grid in (("raw", dem), ("conditioned", dem_c)):
        z, s = reach_profile(grid, tr, stem, dx)
        if z is None:
            continue
        L = float(s[-1])
        drop = float(z[0] - z[-1])
        env = np.minimum.accumulate(z)
        S_env = float(env[0] - env[-1]) / L
        dz = np.diff(z)
        adverse = dz > 0
        out[label] = {
            "reach_km": L / 1000.0, "bed_top_m": float(z[0]),
            "bed_bottom_m": float(z[-1]), "drop_m": drop,
            "S_envelope": S_env,
            "adverse_frac": float(adverse.mean()),
            "adverse_rise_m": float(dz[adverse].sum()) if adverse.any() else 0.0,
        }
        if verbose:
            print(f"  [{label:11s}] reach {L/1000:5.1f} km, bed {z[0]:6.1f} -> "
                  f"{z[-1]:5.1f} m, drop {drop:6.1f} m, envelope S {S_env:.2e}")
            print(f"{'':16s}adverse {adverse.sum()}/{len(dz)} "
                  f"({100*adverse.mean():.0f} %), total rise "
                  f"{float(dz[adverse].sum()) if adverse.any() else 0.0:.1f} m")

    S = out["conditioned"]["S_envelope"]
    out["manning"] = {}
    if verbose:
        print(f"  Manning normal flow on S = {S:.2e}, n = {MANNING_N}:")
    for Q, why in DISCHARGES:
        for W, wl in ((dx, "one cell"), (TRUE_CHANNEL_W_M, "true ~80 m")):
            h, V = manning_normal(Q, W, S)
            out["manning"][f"Q{Q:.0f}_W{W:.0f}"] = {"h_m": h, "V_ms": V}
            if verbose:
                verdict = "OK" if V >= REQUIRED_CELERITY_MS else "SHORT"
                print(f"{'':6s}Q={Q:6.0f} ({why:30s}) W={W:6.1f} ({wl:10s}) "
                      f"h={h:5.2f} m  V={V:5.2f} m/s  {verdict}")
    return out


def self_check() -> None:
    """The claims this diagnostic is cited for must actually hold."""
    r5 = analyse(5, verbose=False)
    r2 = analyse(2, verbose=False)

    s5 = r5["conditioned"]["S_envelope"]
    s2 = r2["conditioned"]["S_envelope"]
    print(f"envelope S: coarsen 5 = {s5:.3e}, coarsen 2 = {s2:.3e}, "
          f"ratio {s2/s5:.4f}")
    assert abs(s2 / s5 - 1.0) < 0.05, (
        f"slope is NOT resolution-independent ({s5:.3e} vs {s2:.3e}) - the "
        f"'real terrain, not a grid artefact' claim does not hold")

    # The reach must be the flood reach, not the whole stem. The whole stem on
    # this river starts above 1000 m; the flood reach starts at the crest.
    assert r5["conditioned"]["bed_top_m"] <= DAM_CREST_M + 1.0, (
        f"reach starts at {r5['conditioned']['bed_top_m']:.1f} m, above the "
        f"{DAM_CREST_M} m crest - the headwaters were not clipped off")
    print(f"reach top {r5['conditioned']['bed_top_m']:.1f} m <= crest "
          f"{DAM_CREST_M} m  OK")

    # The headline: at the discharge the model carries, normal flow is short.
    v_peak = r5["manning"][f"Q1309_W{TRUE_CHANNEL_W_M:.0f}"]["V_ms"]
    v_surge = r5["manning"][f"Q3964_W{TRUE_CHANNEL_W_M:.0f}"]["V_ms"]
    print(f"V at measured peak 1309 m3/s = {v_peak:.2f} m/s "
          f"(need {REQUIRED_CELERITY_MS})")
    print(f"V at official surge 3964 m3/s = {v_surge:.2f} m/s "
          f"(need {REQUIRED_CELERITY_MS})")
    assert v_peak < REQUIRED_CELERITY_MS, (
        "normal flow at the model's own peak already reaches the required "
        "celerity - the 'conveyance is a bound' conclusion is wrong")
    assert v_surge >= REQUIRED_CELERITY_MS, (
        "even the official surge cannot reach the required celerity - the "
        "deficit is larger than a discharge deficit and needs re-diagnosing")

    # Conditioning must only ever lower terrain, so it cannot increase adverse rise.
    for r in (r5, r2):
        assert r["conditioned"]["adverse_rise_m"] <= r["raw"]["adverse_rise_m"] + 1e-6, (
            f"conditioning INCREASED adverse rise at coarsen {r['coarsen']} - "
            f"condition_flowline is supposed to only lower cells")
    print("conditioning never raised adverse rise  OK")
    print("SELF-CHECK PASSED")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="run the self-check")
    ap.add_argument("--coarsen", type=int, nargs="*", default=[5, 2])
    a = ap.parse_args()

    if a.check:
        self_check()
        return

    for c in a.coarsen:
        print(f"\n{'='*74}")
        print(f"coarsen {c}")
        analyse(c)
    print(f"\nmeasured front celerity {MEASURED_CELERITY_MS} m/s; "
          f"arrival gate needs ~{REQUIRED_CELERITY_MS} m/s")


if __name__ == "__main__":
    main()
