"""README figures, all drawn from the served Annamayya run and the live benchmarks.

    python scripts/make_readme_figures.py

Reads data/scenarios/annamayya_compound/* and re-runs the Ritter benchmark for both
solvers. Writes docs/figures/*.png. Nothing here is typed in by hand.
"""
import json
import math
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUN = ROOT / "data" / "scenarios" / "annamayya_compound"
OUT = ROOT / "docs" / "figures"
OUT.mkdir(parents=True, exist_ok=True)

# Reference palette (dataviz skill, light mode)
SURF, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8984", "#e6e5e0"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"

plt.rcParams.update({
    "font.family": "Segoe UI", "font.size": 11, "axes.edgecolor": MUTED,
    "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "axes.spines.top": False, "axes.spines.right": False,
    "figure.facecolor": SURF, "axes.facecolor": SURF, "savefig.facecolor": SURF,
})


def frame(title, sub, w=9, h=4.6):
    fig, ax = plt.subplots(figsize=(w, h), dpi=180)
    fig.text(0.012, 0.965, title, fontsize=14.5, fontweight="bold", color=INK, va="top")
    fig.text(0.012, 0.905, sub, fontsize=10.5, color=INK2, va="top")
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    return fig, ax


def save(fig, name):
    fig.tight_layout(rect=(0, 0, 1, 0.86))
    fig.savefig(OUT / name)
    plt.close(fig)
    print("wrote", OUT / name)


def ritter():
    from src.m4_solvers.validation import run_ritter_benchmark
    from src.m4_solvers.sph_swe import ritter_dam_break_sph
    r = run_ritter_benchmark()
    d = r.detail
    x = np.asarray(d["x_m"]); t = d["t_s"]; h1 = d["h1_m"]
    lo, hi = d["compare_window_m"]
    s, hs = ritter_dam_break_sph(h1_m=h1, t_eval_s=t, domain_m=4000.0, particle_spacing_m=10.0)
    h_sph = s.sample_on(x, hs)
    w = (x >= lo) & (x <= hi)
    he = np.asarray(d["h_analytical_m"]); hf = np.asarray(d["h_simulated_m"])
    rm_fv = float(np.sqrt(np.mean((hf[w] - he[w]) ** 2)))
    rm_sph = float(np.sqrt(np.mean((h_sph[w] - he[w]) ** 2)))

    fig, ax = frame("Ritter (1892) dam break: both solvers against the exact solution",
                    f"{h1:.0f} m of still water released onto a dry, flat, frictionless bed; "
                    f"depth at t = {t:.0f} s. Errors over x = {lo:.0f} to {hi:.0f} m.")
    m = (x > -1200) & (x < 1100)
    ax.plot(x[m], he[m], color=INK, lw=2.4, label="Exact (Ritter)", zorder=3)
    ax.plot(x[m], hf[m], color=BLUE, lw=2, label=f"FloodSense 2D finite-volume: RMSE {rm_fv:.3f} m", zorder=4)
    ax.plot(x[m], h_sph[m], color=ORANGE, lw=2, label=f"SWE-SPH 1D prototype: RMSE {rm_sph:.2f} m", zorder=2)
    ax.axvspan(lo, hi, color="#2a78d6", alpha=0.05, lw=0)
    ax.set_xlabel("distance from the dam [m]"); ax.set_ylabel("water depth [m]")
    ax.set_ylim(-0.3, h1 * 1.12)
    ax.legend(frameon=False, loc="upper right", fontsize=10)
    save(fig, "ritter_benchmark.png")
    return {"rmse_fv": rm_fv, "rmse_sph": rm_sph, "front_rel_err": d["front_relative_error"], "t": t}


def hydrograph():
    h = json.loads((RUN / "hydrograph.json").read_text(encoding="utf-8"))
    t = np.asarray(h["central"]["t_s"]) / 3600.0
    q = np.asarray(h["central"]["Q_m3s"])
    fig, ax = frame("Annamayya 2021: the release that drives the flood",
                    "Prescribed from reported discharges (SANDRP), not computed from a breach: "
                    "this run is a forced-hydrograph inundation.")
    ax.fill_between(t, q, color=BLUE, alpha=0.12, lw=0)
    ax.plot(t, q, color=BLUE, lw=2)
    i = int(np.argmax(q))
    ax.plot(t[i], q[i], "o", ms=8, color=BLUE, mec=SURF, mew=2, zorder=5)
    ax.annotate(f"peak {q[i]:,.0f} m³/s", (t[i], q[i]), xytext=(12, -4),
                textcoords="offset points", color=INK, fontsize=10.5)
    ax.set_xlabel("hours after the embankment washed out (06:30 IST)"); ax.set_ylabel("outflow [m³/s]")
    ax.set_ylim(0, q.max() * 1.15)
    save(fig, "annamayya_release.png")
    return {"q_peak": float(q[i]), "t_peak_h": float(t[i])}


def arrivals():
    v = json.loads((RUN / "validation_arrivals.json").read_text(encoding="utf-8"))
    rows = v["results"]
    names = [r["name"].split(" (")[0].replace(" railway bridge", " rly bridge") for r in rows]
    fig, ax = frame("Flood arrival: model against reported times, Annamayya 2021",
                    f"{v['summary'].replace(' historical', '')}; depths {v['depth_matches']} of 3 in range. "
                    "The modelled front is ~3.3× too slow.", h=4.4)
    y = np.arange(len(rows))[::-1]
    for yi, r in zip(y, rows):
        a, b = r["obs_arrival_min_range"]
        ax.plot([a, b], [yi, yi], color=INK2, lw=7, solid_capstyle="round", alpha=0.35)
        ax.plot(r["modeled_arrival_min"], yi, "o", ms=9, color=ORANGE, mec=SURF, mew=2, zorder=5)
        ax.annotate(f"+{r['arrival_signed_error_min']:.0f} min", (r["modeled_arrival_min"], yi),
                    xytext=(9, -4), textcoords="offset points", fontsize=9.5, color=INK2)
    ax.set_yticks(y); ax.set_yticklabels(names)
    ax.set_xlabel("minutes after the embankment washed out (06:30)")
    ax.plot([], [], color=INK2, lw=7, alpha=0.35, label="reported window")
    ax.plot([], [], "o", color=ORANGE, label="modelled arrival")
    ax.legend(frameon=False, loc="upper right", fontsize=10)
    ax.grid(axis="y", visible=False)
    save(fig, "arrival_validation.png")
    return {"late_min": [r["arrival_signed_error_min"] for r in rows], "summary": v["summary"],
            "depth_matches": v["depth_matches"]}


def leave_by():
    d = json.loads((RUN / "evac_routes.geojson").read_text(encoding="utf-8"))
    rows = [r for r in d["settlements"] if r["modes"]["foot"]["status"] == "leave_by"]
    rows.sort(key=lambda r: r["priority_rank"])
    fig, ax = frame("Last safe departure by settlement, on this run",
                    "Latest minute a walking or driving route to dry ground still exists. "
                    "The modelled flood is slow, so real leave-by times were earlier.",
                    h=5.6)
    y = np.arange(len(rows))[::-1]
    for yi, r in zip(y, rows):
        f, v = r["modes"]["foot"]["leave_by_min"], r["modes"]["vehicle"]["leave_by_min"]
        ax.plot([f, v], [yi, yi], color=GRID, lw=3, zorder=1)
        ax.plot(f, yi, "o", ms=8, color=BLUE, mec=SURF, mew=2, zorder=4)
        ax.plot(v, yi, "o", ms=8, color=ORANGE, mec=SURF, mew=2, zorder=3)
        if r["water_arrival_min"] is not None:
            ax.plot(r["water_arrival_min"], yi, marker="|", ms=14, mew=2.4, color=INK, zorder=5)
    ax.set_yticks(y)
    ax.set_yticklabels([f"#{r['priority_rank']} {r['village_name'].split(' (')[0]}" for r in rows])
    ax.set_xlabel("minutes after the embankment washed out")
    ax.plot([], [], "o", color=BLUE, label="leave by, on foot")
    ax.plot([], [], "o", color=ORANGE, label="leave by, vehicle")
    ax.plot([], [], marker="|", ms=12, mew=2.4, ls="", color=INK, label="water reaches the village")
    ax.legend(frameon=False, loc="lower right", bbox_to_anchor=(1.0, 1.0), ncol=3, fontsize=10,
              borderaxespad=0.2, handletextpad=0.4, columnspacing=1.2)
    ax.grid(axis="y", visible=False)
    save(fig, "leave_by.png")
    return {"n": len(rows), "open": sum(r["modes"]["foot"]["status"] == "open" for r in d["settlements"]),
            "variants": len(d["features"]), "mainland": d["mainland_nodes"], "shelters": d["shelters_on_mainland"]}


def roads():
    r = json.loads((RUN / "roads_timeline.geojson").read_text(encoding="utf-8"))
    c = np.sort([f["properties"]["cut_time_min"] for f in r["features"]
                 if f["properties"]["cut_time_min"] is not None])
    n = len(r["features"])
    fig, ax = frame("Road links cut by the flood over time",
                    f"{len(c)} of {n:,} OSM road links reach 0.30 m of water at their midpoint "
                    "(bridges 3.0 m) during the run.")
    ax.step(np.concatenate([[0], c]), np.arange(len(c) + 1), where="post", color=BLUE, lw=2)
    ax.set_xlabel("minutes after the embankment washed out"); ax.set_ylabel("links cut (cumulative)")
    ax.set_xlim(0, max(c) * 1.05); ax.set_ylim(0, len(c) * 1.12)
    save(fig, "roads_cut.png")
    return {"cut": int(len(c)), "total": n, "first": float(c[0]), "last": float(c[-1])}


if __name__ == "__main__":
    res = {"ritter": ritter(), "release": hydrograph(), "arrivals": arrivals(),
           "leave_by": leave_by(), "roads": roads()}
    (OUT / "figures_numbers.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))
