"""Route the 19 Nov 2021 Annamayya flood down the Cheyyeru from a SOURCED hydrograph.

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
This is a **forced-hydrograph inundation run**, not a reservoir-drainage
simulation. It is labelled that way in its output and must stay labelled.

Why it has to be. Copernicus GLO-30 over this AOI is a surface model captured
with the reservoir full: the DEM at the dam site is a flat **192.50 m** water
plane, and the published crest is **206.0 m**. There is no dam in the terrain
and no valley floor beneath the pool, so the impoundment cannot be emplaced,
cannot be confined, and cannot be drained through a breach. Measured: at 206 m,
740.23 km2 of this domain lies at or below the crest across a valley 25.1 km
wide, against a dam_body footprint of 366 m. That is why the geometry gate
refuses the scenario, and it is a DEM limitation, not a manifest defect.

What can still be done honestly is route the water that is known to have been
released, over terrain that IS resolved downstream of the dam, and report where
it goes. Every number below carries its source. Nothing is tuned to an outcome.

    python scripts/route_annamayya.py --coarsen 3
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import rasterio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data_fetcher import SCENARIOS, build_rivers         # noqa: E402
from src.m2_geometry.dem_utils import condition_dem          # noqa: E402
from src.m4_solvers.swe_2d import run_2d_swe_simulation      # noqa: E402

# The solver reports progress through `logging`. Without this the run is silent
# for its whole wall time and looks hung -- which is exactly how it looked the
# first time it was run.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

CUSEC = 0.0283168                     # 1 cusec -> m3/s

# ── Sourced event quantities ─────────────────────────────────────────────────
# SANDRP, "Andhra Pradesh: Dam Induced Flood Disaster in November 2021"; press
# reporting of the AP Water Resources Dept figures; Annamayya Dam Break Analysis
# (Ravi). Classification is recorded with each figure and travels to the output.
# Corrected 2026-09-13 against data/evidence/annamayya_event_evidence.json, which
# is the authority for this scenario. Three things were wrong here:
#
#   * `pincha_release_m3s` carried 1.17 lakh cusec. NOTHING in the evidence file
#     supports it -- the only Pincha discharge figure is `pincha_surge_peak_m3s`,
#     140,000 cfs with a 120,000-160,000 range, and 117,000 sits BELOW that range's
#     own floor. Deleted, not adjusted.
#   * Two figures were classified OBSERVED. The evidence file classifies both
#     OFFICIAL_ESTIMATE. An estimate promoted to an observation is the provenance
#     defect this file exists to avoid.
#   * The peak inflow at Annamayya was carried as ONE unattributed number. It is
#     three, by agency, and the evidence file preserves the 2x spread deliberately
#     under `usage: "Inflow hydrograph ensemble bounds"`. They are carried apart.
#
# READ THIS BEFORE USING THE AGENCY FIGURES. They are peak TOTAL INFLOW AT
# ANNAMAYYA -- what the routing must PRODUCE, not what it consumes. The forcing is
# `pincha_surge_peak_m3s`, and there is only one of those.
SOURCES = {
    "gross_storage_mcm": {
        "value": 2.24 * 28.3168,          # 2.24 TMC = 63.43 MCM
        "classification": "OFFICIAL_ESTIMATE",
        "source": ("Annamayya gross capacity 2.24 TMC / 63.43 MCM -- National "
                   "Register of Large Dams (CWC) / AP Irrigation "
                   "(annamayya_gross_volume_mcm)"),
    },
    "pincha_surge_peak_m3s": {
        "value": 3964.4,
        "value_cfs": 140000.0,
        "range_cfs": [120000.0, 160000.0],
        "classification": "OFFICIAL_ESTIMATE",
        "source": "AP Irrigation Official Flood Report (Nov 2021), EVD-05",
        "usage": "1D channel routing inflow -- THE Stage-1 forcing",
    },
    "peak_total_inflow_annamayya_m3s": {
        "mha_official":        6412.0,
        "cwc_appraisal":       9065.0,
        "iisc_reconstruction": 12740.0,
        "classification": "OFFICIAL_ESTIMATE",
        "source": ("MHA Report D692 vs CWC Technical Appraisal (2021) vs IISc "
                   "(2023), EVD-15 -- range preserved, not collapsed"),
        "usage": ("Inflow hydrograph ensemble bounds AT ANNAMAYYA. This is the "
                  "routing's OUTPUT to be checked against, never its input."),
        "arm_in_use": "iisc_reconstruction",
        "arm_note": ("IISc 12,740 m3/s is the UPPER envelope of the three. It is "
                     "deliberately conservative and over-predicts by design. Label "
                     "it 'IISc upper arm' wherever it appears; it is NOT the best "
                     "estimate. User decision, 2026-09-13."),
    },
    "spillway_operating_discharge_m3s": {
        "value": 4135.8,
        "value_cfs": 146056.0,
        "operating_gates": 4,
        "jammed_gates": [4],
        "classification": "OFFICIAL_ESTIMATE",
        "source": "MHA National Situation Report D692 (EVD-12/14)",
    },
    "outflow_capacity_m3s": {
        "value": 2.17e5 * CUSEC,
        "classification": "UNMATCHED_IN_EVIDENCE_FILE",
        "source": "Annamayya rated discharge capacity 2.17 lakh cusec",
        "note": ("2.17 lakh cusec matches NEITHER of the evidence file's two "
                 "spillway figures -- operating 146,056 cfs nor design 285,000 cfs. "
                 "Retained because nothing in this script reads it, and flagged so "
                 "it is not cited. Resolve or delete it; do not quote it."),
    },
}
BREACH_LON, BREACH_LAT = 79.02128, 14.21059      # SCENARIOS['annamayya']
PINCHA_LON, PINCHA_LAT = 78.99956, 13.90890      # upstream_structure, EVD-03
T0_IST = "2021-11-19T06:30:00+05:30"             # full 336 m earthen section washout


def _trapz(y, x) -> float:
    return float(np.trapezoid(y, x)) if hasattr(np, "trapezoid") else float(np.trapz(y, x))


def catchment_runoff(t_s: np.ndarray) -> np.ndarray:
    """The catchment's own contribution, from the sourced Gaussian pulse.

    `SCENARIOS['annamayya']['cascade']['catchment_runoff']` -- base 800 m3/s,
    peak 3,800 m3/s, offset -1,800 s, sigma 5,400 s, derived from EVD-01's
    180 mm rainfall. `t_s` is on the EVENT CLOCK (t=0 is the 06:30 washout),
    because `peak_offset_s` is.

    This replaces the deleted "continuing inflow held at the Pincha release
    rate" term, which was built on the 1.17 lakh cusec figure no source
    supports. This one has a source and a range.
    """
    cfg = SCENARIOS["annamayya"]["cascade"]["catchment_runoff"]
    base, peak = float(cfg["base_m3s"]), float(cfg["peak_m3s"])
    off, sig = float(cfg["peak_offset_s"]), float(cfg["sigma_s"])
    return base + (peak - base) * np.exp(-0.5 * ((t_s - off) / sig) ** 2)


def build_hydrograph(hours: float = 12.0, dt_s: float = 300.0):
    """Breach release + the catchment inflow still arriving behind it.

    Two superposed parts, because the river did not stop when the dam failed:

      1. **Storage release** -- 63.43 MCM leaving through the washed-out 336 m
         earthen section. Shaped as a rising limb to the peak then an
         exponential recession, and RESCALED so the integral equals the sourced
         storage exactly rather than whatever the shape happened to give.
      2. **Continuing inflow** -- `catchment_runoff` above.

    The peak of part 1 is the selected arm of the three-agency peak-total-inflow
    ensemble. Decision of 2026-09-13: the **IISc upper arm, 12,740 m3/s**. That
    is the UPPER envelope of MHA 6,412 / CWC 9,065 / IISc 12,740 and
    over-predicts by design -- it is not the best estimate, and anything derived
    from this hydrograph must be labelled "IISc upper arm". The previous value
    here was 3.2 lakh cusec = 9,061 m3/s, i.e. the CWC arm, carried as though it
    were the only figure.

    NOTE ON SCOPE: this is the STAGE-2-STANDALONE forcing, which prescribes what
    leaves Annamayya. The two-stage chain does not use it -- Stage 2 is forced by
    the Q(t) Stage 1 measures at the handoff section. Kept so the single-stage
    run that produced the recorded baseline remains reproducible.
    """
    t = np.arange(0.0, hours * 3600.0 + dt_s, dt_s)
    arm = SOURCES["peak_total_inflow_annamayya_m3s"]["arm_in_use"]
    q_peak = float(SOURCES["peak_total_inflow_annamayya_m3s"][arm])
    t_peak = 45.0 * 60.0                       # rise to peak in 45 min
    tau = 2.5 * 3600.0                         # recession timescale

    release = np.where(t <= t_peak,
                       q_peak * (t / t_peak),
                       q_peak * np.exp(-(t - t_peak) / tau))
    target = SOURCES["gross_storage_mcm"]["value"] * 1e6
    release *= target / _trapz(release, t)     # integral == sourced storage

    q = release + catchment_runoff(t)
    return t, q, _trapz(q, t)


# -----------------------------------------------------------------------------
# Grid, handoff section, and Stage 1
# -----------------------------------------------------------------------------

def build_grid(coarsen: int):
    """The one grid both stages run on, so the handoff needs no reprojection.

    Coarsening takes the block MINIMUM, not the mean: averaging a narrow channel
    with its banks fills it in. The conditioner's floor is the sourced
    `dem_floor_m`, not a percentile -- see SCENARIOS['annamayya'].
    """
    dem_path = ROOT / "data" / "dem" / "annamayya_dem.tif"
    with rasterio.open(dem_path) as src:
        raw = src.read(1).astype(float)
        tr, crs = src.transform, src.crs
    if coarsen > 1:
        c = coarsen
        ny, nx = raw.shape[0] // c * c, raw.shape[1] // c * c
        raw = raw[:ny, :nx].reshape(ny // c, c, nx // c, c)
        with np.errstate(all="ignore"):
            raw = np.nanmin(np.where(raw > -1000, raw, np.nan), axis=(1, 3))
        tr = rasterio.Affine(tr.a * c, tr.b, tr.c, tr.d, tr.e * c, tr.f)
    dem, repaired = condition_dem(
        raw, nodata=-9999.0, floor_m=float(SCENARIOS["annamayya"]["dem_floor_m"]))
    return dem, tr, crs, abs(tr.a), abs(tr.e), repaired


def locate_handoff(dem, tr, crs, half_width_cells: int = 12,
                   plane_m: float = 192.50, plane_tol_m: float = 0.5) -> dict:
    """Find the upstream edge of the reservoir water plane, and a section there.

    Copernicus GLO-30 over this AOI is a DSM captured with Annamayya full: the
    bed under the pool is a flat `plane_m` water surface, not terrain. Depth,
    velocity and flow area taken anywhere inside it are properties of that
    artefact. So the handoff -- the section where Stage 1 hands Q(t) to Stage 2
    -- has to sit at the LAST REAL BED CELL upstream of the plane, and that
    location is measured here rather than assumed.

    The section is the line through that cell perpendicular to the river's local
    tangent. Q across it is h*(u.t_hat) integrated over the section, i.e.
    measured from the solver's own state -- never prescribed.
    """
    from shapely.geometry import LineString, Point
    from pyproj import Transformer

    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    pincha = Point(fwd.transform(PINCHA_LON, PINCHA_LAT))
    dam = Point(fwd.transform(BREACH_LON, BREACH_LAT))

    rivers = build_rivers("annamayya")
    if rivers.attrs.get("bbox_stale"):
        # The handoff section is a PHYSICS BOUNDARY sited off this layer. A layer
        # fetched for a different AOI puts it somewhere else, silently. Measured
        # 2026-09-13: the cached layer was the 2026-09-05 fetch on the
        # pre-widening bbox and nothing said so.
        raise RuntimeError(
            "the OSM river layer is stale for this bbox and Stage 1 sites its "
            "handoff section on it. Refetch it (build_rivers force=True) when "
            "Overpass is reachable; do not run Stage 1 on a stale river network.")
    rivers = rivers.to_crs(crs)
    named = [g for n, g in zip(rivers.get("name"), rivers.geometry)
             if str(n) == "Cheyyeru"]
    if not named:
        raise RuntimeError("no OSM reach named Cheyyeru in this domain")
    # Both reaches are named "Cheyyeru". The upstream one is the reach that both
    # touches the dam AND lies nearest Pincha.
    reach = min(named, key=lambda g: min(Point(g.coords[0]).distance(dam),
                                         Point(g.coords[-1]).distance(dam))
                                     + g.distance(pincha))
    if Point(reach.coords[0]).distance(pincha) > Point(reach.coords[-1]).distance(pincha):
        reach = LineString(list(reach.coords)[::-1])       # s = 0 at the Pincha end

    step = min(abs(tr.a), abs(tr.e)) / 2.0
    last_real = None
    first_plane = None
    for s_m in np.arange(0.0, reach.length, step):
        pt = reach.interpolate(s_m)
        row, col = rasterio.transform.rowcol(tr, pt.x, pt.y)
        if not (0 <= row < dem.shape[0] and 0 <= col < dem.shape[1]):
            continue
        if abs(float(dem[row, col]) - plane_m) <= plane_tol_m:
            first_plane = (s_m, row, col)
            break
        last_real = (s_m, row, col)
    if first_plane is None:
        raise RuntimeError("no %.2f m water plane found along the Cheyyeru" % plane_m)
    if last_real is None:
        raise RuntimeError("the water plane starts at the reach head; no real bed upstream")

    s_m, row, col = last_real
    pt = reach.interpolate(s_m)
    # local flow direction (tangent), then the section runs along its normal
    d = min(200.0, reach.length / 100.0)
    a = reach.interpolate(max(0.0, s_m - d))
    b = reach.interpolate(min(reach.length, s_m + d))
    tx, ty = b.x - a.x, b.y - a.y
    tlen = float(np.hypot(tx, ty)) or 1.0
    tx, ty = tx / tlen, ty / tlen
    nx_, ny_ = -ty, tx

    spacing = min(abs(tr.a), abs(tr.e))
    seen, cells = set(), []
    for k in range(-half_width_cells, half_width_cells + 1):
        x, y = pt.x + k * spacing * nx_, pt.y + k * spacing * ny_
        r_, c_ = rasterio.transform.rowcol(tr, x, y)
        if not (0 <= r_ < dem.shape[0] and 0 <= c_ < dem.shape[1]):
            continue
        if (r_, c_) in seen:
            continue
        seen.add((r_, c_))
        cells.append({"row": int(r_), "col": int(c_), "k": int(k),
                      "bed_m": float(dem[r_, c_])})

    lon, lat = inv.transform(pt.x, pt.y)
    lon_p, lat_p = inv.transform(*rasterio.transform.xy(tr, first_plane[1], first_plane[2]))
    return {
        "reach_length_km": reach.length / 1000.0,
        "s_along_reach_km": s_m / 1000.0,
        "distance_to_dam_along_reach_km": (reach.length - s_m) / 1000.0,
        "row": int(row), "col": int(col),
        "lat": float(lat), "lon": float(lon),
        "bed_m": float(dem[row, col]),
        "plane_first_cell": {"s_km": first_plane[0] / 1000.0,
                             "row": int(first_plane[1]), "col": int(first_plane[2]),
                             "lat": float(lat_p), "lon": float(lon_p),
                             "bed_m": float(dem[first_plane[1], first_plane[2]])},
        "flow_direction_utm": [float(tx), float(ty)],
        # The SAME direction expressed in GRID axes, which is what the solver's
        # u and v are. `u` is the velocity along +col and `v` the velocity along
        # +row -- and on a north-up raster (tr.e < 0) +row is SOUTHWARD, so a
        # northbound river has v < 0. Projecting the UTM tangent straight onto
        # (u, v) therefore reads the flux BACKWARDS: measured -5.75 MCM through
        # this section on the 12 h run before this was fixed.
        "flow_direction_grid": [float(tx * np.sign(tr.a)), float(ty * np.sign(tr.e))],
        "section_direction_utm": [float(nx_), float(ny_)],
        "section_spacing_m": float(spacing),
        "section_cells": cells,
        "reach_head_to_pincha_km": float(Point(reach.coords[0]).distance(pincha) / 1000.0),
        # The Cheyyeru's head is ~8.8 km from the Pincha ring bund because the
        # Cheyyeru BEGINS at Rayavaram, where the Pincha and Bahuda headstreams
        # merge. That is NOT an unmapped gap: OSM maps the Pincha River itself
        # (way 148491940, 54.6 km) and it passes ~0.1 km from the ring bund.
        # A 2026-09-05 river cache built before the bbox was extended south did
        # not contain it, and that absence was briefly mistaken for the river
        # not existing. Report the nearest mapped river of ANY name so the same
        # mistake cannot be made from this output.
        "nearest_mapped_river_to_pincha_km": float(
            min((g.distance(pincha) for g in rivers.geometry if g.length > 100.0),
                default=float("nan")) / 1000.0),
    }


def section_discharge(res, handoff: dict):
    """Q(t) through the handoff section, from the solver's own h, u, v.

    Q = sum over section cells of h * (u . t_hat) * spacing, where t_hat is the
    river's local flow direction. This is a MEASUREMENT of the saved state, not
    a prescribed hydrograph -- and the solver is untouched to get it.
    """
    # GRID-axis components: u is along +col, v is along +row. See
    # locate_handoff for why the UTM tangent cannot be used directly.
    tx, ty = handoff["flow_direction_grid"]
    sp = handoff["section_spacing_m"]
    cells = handoff["section_cells"]
    out = {"t_s": [], "Q_m3s": [], "h_max_m": [], "speed_max_ms": [],
           "wetted_width_m": [], "flow_area_m2": [], "wse_max_m": []}
    for ts, h, u, v in zip(res.times_s, res.depth_grids, res.u_grids, res.v_grids):
        q = 0.0
        hmax = 0.0
        smax = 0.0
        wet = 0
        area = 0.0
        wse = None
        for c in cells:
            r_, c_ = c["row"], c["col"]
            hh = float(h[r_, c_])
            if hh <= 1e-6:
                continue
            un = float(u[r_, c_]) * tx + float(v[r_, c_]) * ty
            q += hh * un * sp
            area += hh * sp
            wet += 1
            hmax = max(hmax, hh)
            smax = max(smax, float(np.hypot(u[r_, c_], v[r_, c_])))
            w_ = c["bed_m"] + hh
            wse = w_ if wse is None else max(wse, w_)
        out["t_s"].append(float(ts))
        out["Q_m3s"].append(float(q))
        out["h_max_m"].append(float(hmax))
        out["speed_max_ms"].append(float(smax))
        out["wetted_width_m"].append(float(wet * sp))
        out["flow_area_m2"].append(float(area))
        out["wse_max_m"].append(wse)
    return out


def _spinup(dem, dx, dy, manning, a):
    """Fill the channel with the sourced pre-event baseflow before the release.

    WHY THIS IS A DEFECT FIX AND NOT A KNOB
    ---------------------------------------
    The run starts from a COMPLETELY DRY BED. The Cheyyeru on 19 Nov 2021 was
    not dry -- 180 mm of rain (EVD-01) was already falling and the sourced
    catchment baseflow is `cascade.catchment_runoff.base_m3s = 800 m3/s`.

    It changes the physics, not just the initial volume. A wave entering a
    channel that already carries water propagates at roughly sqrt(g*h) + u --
    metres per second. A wave advancing over DRY ground has to wet every cell
    first, and on a 152 m grid with a floodplain roughness that is a much slower
    process. The 24 h run reached the Pennar confluence at T+840 min, a front
    speed of 1.07 m/s over the 54 km stem, against a documentary window of
    120-300 min (3.0-7.5 m/s). A dry bed is a plausible reason for that gap.

    No geometry and no depth is invented here: a constant, SOURCED 800 m3/s is
    injected at the same release cell and the SOLVER decides where the water
    goes and how deep it sits. The resulting volume is reported and enters the
    run as `initial_depth`, which `mass_closure` accounts for separately from
    the release (`initial_m3` vs `injected_m3`), so it cannot be mistaken for
    part of the 63.43 MCM.

    This does NOT tune anything to an arrival window. If it does not move the
    arrivals, that is the finding.
    """
    if a.spinup_hours <= 0:
        return None

    base = float(SCENARIOS["annamayya"]["cascade"]["catchment_runoff"]["base_m3s"])
    print(f"\nSPIN-UP: {base:.0f} m3/s sourced baseflow for {a.spinup_hours:.1f} h "
          f"to wet the channel before the release")
    t_su = np.array([0.0, a.spinup_hours * 3600.0])
    q_su = np.array([base, base])

    from pyproj import Transformer as _T
    res = run_2d_swe_simulation(
        elevation_grid=dem, dx_m=dx, dy_m=dy,
        inflow_x_idx=a._ix, inflow_y_idx=a._iy,
        hydrograph_t_s=t_su, hydrograph_Q_m3s=q_su,
        total_duration_s=a.spinup_hours * 3600.0,
        save_interval_s=a.spinup_hours * 3600.0,
        manning_n=manning,
        scenario_name="annamayya_spinup",
        stop_when_quiescent=False,
    )
    h0 = res.depth_grids[-1]
    vol = float(h0.sum() * dx * dy)
    wet = int((h0 > 0.05).sum())
    print(f"  spin-up left {vol/1e6:,.2f} MCM standing in {wet} cells "
          f"({wet*dx*dy/1e6:,.1f} km2), max depth {h0.max():.2f} m")
    print(f"  injected {base*a.spinup_hours*3600/1e6:,.2f} MCM over the spin-up; "
          f"this enters the run as initial_depth and is accounted as initial_m3, "
          f"NOT as part of the 63.43 MCM release")
    return h0


def _condition_terrain(dem, wall_mask, tr, crs, release_bed_m, a):
    """Apply the two terrain steps run_pipeline does and this script never did.

    INVARIANTS S1 puts terrain modification "between the geometry gate and
    open_river_outlets, nowhere else", and both of these live on that production
    path. `route_annamayya.py` has always skipped both, which is why its domain
    measures `outflow 0.000e+00` -- sealed -- and why its front has to climb
    every adverse bed rise a 30 m surface model invents along the valley floor.

    Neither invents terrain. `open_river_outlets` lowers wall cells only where a
    MAPPED river crosses the data footprint, to the bed the river already had
    there. `condition_flowline` only ever LOWERS, never raises, and refuses a cut
    deeper than its 25 m cap.

    Returns (dem, channel_mask, reports).
    """
    reports = {}
    channel_mask = None
    if not (a.condition_flowline or a.open_outlets):
        return dem, channel_mask, reports

    from src.m2_geometry.dem_utils import condition_flowline, open_river_outlets

    rivers = build_rivers("annamayya")
    if rivers.attrs.get("bbox_stale"):
        raise RuntimeError(
            "--condition-flowline/--open-outlets modify TERRAIN from the river "
            "layer, and that layer is stale for this bbox. Refetch it first: "
            "carving a channel along the wrong river is not recoverable from the "
            "output.")
    geoms = list(rivers.to_crs(crs).geometry)

    if a.open_outlets:
        dem, rep = open_river_outlets(dem, wall_mask, tr, geoms,
                                      source_elev_m=float(release_bed_m))
        reports["outlets"] = rep
        if rep.get("available"):
            print(f"outlets: {rep['outlets']} opened, {rep['cells_opened']} cells, "
                  f"bed elevations {rep['outlet_beds_m']} (source {rep['source_elev_m']} m)"
                  f" — the domain can now drain")
        else:
            print(f"outlets: NOT opened — {rep.get('reason')}")

    if a.condition_flowline:
        dem, channel_mask, rep = condition_flowline(dem, tr, geoms)
        reports["flowline"] = rep
        if rep.get("available"):
            # Print the report as reported. Guessing key names printed "?" for
            # the two numbers that matter most -- how deep the cut went and how
            # much terrain it removed -- which is exactly what a reader needs to
            # judge whether the conditioning was reasonable.
            print("flowline: " + ", ".join(f"{k}={v}" for k, v in sorted(rep.items())
                                           if k != "available"))
        else:
            print(f"flowline: NOT conditioned — {rep.get('reason')}")

    return dem, channel_mask, reports


def _roughness_field(dem, tr, crs, a):
    """Uniform floodplain n, with the MAPPED channel dropped to n=0.035.

    Left off by default so the change is a deliberate, reported one. The
    measurement in `apply_channel_roughness`'s own docstring is why it exists:
    the CHANNEL n is what moves the front, and this run had been giving the
    channel the floodplain's 0.045 -- rough for a sand/gravel river, and the
    wrong lever to be holding.
    """
    if not a.channel_roughness:
        return float(a.manning)

    from src.m4_solvers.roughness import apply_channel_roughness, CHANNEL_MANNING_N

    rivers = build_rivers("annamayya")
    if rivers.attrs.get("bbox_stale"):
        # Stage 2 routes DOWNSTREAM of the dam, and the downstream Cheyyeru stem
        # (54.0 km, ending at the Pennar confluence) IS present in the cached
        # layer -- it was verified in the domain. What the stale layer is missing
        # is the Pincha arm UPSTREAM, which this stage never touches. So this is
        # usable here and refused in locate_handoff, and the difference is the
        # reach each one depends on, not a difference in standards.
        logger.warning("channel roughness is being built from a river layer that "
                       "is STALE for this bbox. Valid here (the downstream stem is "
                       "present and verified); it would NOT be valid upstream.")
    rivers = rivers.to_crs(crs)
    grid = np.full(dem.shape, float(a.manning), dtype=np.float64)
    grid, report = apply_channel_roughness(
        grid, list(rivers.geometry), tr, n_channel=CHANNEL_MANNING_N,
        channel_mask=getattr(a, "_channel_mask", None))
    if not report.get("available"):
        raise RuntimeError("--channel-roughness asked for, but the channel does not "
                           f"resolve: {report.get('reason')}")
    print(f"roughness: floodplain n={a.manning:.3f}, channel n={CHANNEL_MANNING_N:.3f} "
          f"on {report['channel_cells']} cells ({100*report['channel_fraction']:.2f} %), "
          f"which averaged n={report['mean_n_replaced']:.3f} before")
    return grid


def village_exposure(vil, max_depth_grid, arrival_grid, transform, wet_m: float = 0.3):
    """Per-village depth, arrival and population at risk, over each POLYGON.

    This replaced a single-cell sample at the polygon centroid:

        c_ = r.geometry.centroid
        cc = int((c_.x - tr.c) / tr.a); rw = int((c_.y - tr.f) / tr.e)
        d_ = float(res.max_depth_grid[rw, cc])

    At 152 m that is 2.32 ha standing in for a settlement kilometres across, and it
    was wrong in the direction that matters. Measured on `annamayya_stage2_wide`:
    **11 of 23 settlements reported NOT REACHED with part of their polygon under
    water** — Lebaka 60 % flooded, Obili 44 % at 3.13 m, Ramachandrapuram 50 % at
    4.44 m — and **30,571 people went unscored**. Gundlur is the worst of them:
    EVD-25 reports 2.0-4.0 m there, the model produced 3.11 m over the polygon, and
    the centroid sample threw it away — so a reproduced observation read as a miss.

    Semantics are taken from `src/m5_exposure/exposure.py::compute_village_exposure`,
    which `run_pipeline` already uses and which always did this correctly. The
    centroid version was a parallel reimplementation of it — the near-miss this
    project's INVARIANTS file exists to prevent. In particular `pop_at_risk` is
    `pop_total * flooded_area_frac`, a uniform-density apportionment and therefore
    PROXY, NOT the whole population the moment one cell wets.

    Arrival is the EARLIEST arrival among the polygon's wet cells: when water
    reached the village, not when it reached the cell under its centroid.
    """
    import rasterio.features

    ny, nx = max_depth_grid.shape
    out = {k: [] for k in ("max_depth_m", "mean_depth_m", "flooded_area_frac",
                           "inundated", "water_arrival_min", "pop_at_risk",
                           "exposure_status")}
    for _, r in vil.iterrows():
        geom = r.geometry
        mask = np.zeros((ny, nx), dtype=bool)
        status = "OK"
        if geom is None or geom.is_empty:
            status = "NO_GEOMETRY"
        else:
            try:
                # all_touched=False — a cell counts when its CENTRE is inside the
                # polygon. This matches `rasterio.mask.mask`, which is what
                # `m5_exposure.compute_village_exposure` uses, and the two must
                # agree or the same village scores differently depending on which
                # code path ran. all_touched=True adds a one-cell ring around
                # every polygon, which at 152 m is ~150 m of borrowed ground:
                # measured on Gundlur it moved flooded fraction 33 % -> 64 %.
                mask = rasterio.features.geometry_mask(
                    [geom.__geo_interface__], out_shape=(ny, nx),
                    transform=transform, invert=True, all_touched=False)
            except Exception as exc:                               # noqa: BLE001
                status = "MASK_FAILED"
                logger.warning("village mask failed for %s: %s",
                               r.get("village_name", "?"), exc)
        if status == "OK" and not mask.any():
            # No cell centre falls inside. Either the settlement is smaller than
            # one 2.3 ha cell, or it is off the grid entirely. Tell those apart:
            # a sub-cell village gets the cell it sits in, flagged, because
            # reporting it dry would be the centroid bug wearing a new hat.
            c = geom.centroid
            rw = int((c.y - transform.f) / transform.e)
            cc = int((c.x - transform.c) / transform.a)
            if 0 <= rw < ny and 0 <= cc < nx:
                mask[rw, cc] = True
                status = "SUBCELL"
            else:
                status = "OUTSIDE_DOMAIN"

        d = max_depth_grid[mask] if mask.any() else np.zeros(0)
        d = d[np.isfinite(d)]
        wet = d >= wet_m
        max_d = float(d.max()) if d.size else 0.0
        mean_d = float(d[wet].mean()) if wet.any() else 0.0
        frac = float(wet.sum() / d.size) if d.size else 0.0

        a_min = None
        if wet.any() and arrival_grid is not None:
            a = arrival_grid[mask & (max_depth_grid >= wet_m)]
            a = a[np.isfinite(a) & (a >= 0)]
            if a.size:
                a_min = round(float(a.min()) / 60.0, 1)

        out["max_depth_m"].append(round(max_d, 2))
        out["mean_depth_m"].append(round(mean_d, 2))
        out["flooded_area_frac"].append(round(frac, 4))
        out["inundated"].append(bool(wet.any()))
        out["water_arrival_min"].append(a_min)
        out["pop_at_risk"].append(int(round(float(r.get("pop_total") or 0) * frac)))
        out["exposure_status"].append(status)
    return out


def write_exports(out: Path, vil, max_depth_grid, transform, to_wgs84, *,
                  released_volume_m3: float, peak_q_m3s: float, t_s: float):
    """Write the run's .shp / .kml / CAP exports and return their paths.

    PS 26161 deliverable (iii) names these formats explicitly. `run_pipeline`
    has always written them; this script never did, so the one Indian run we
    actually demonstrate had no .shp or .kml on disk.

    TWO layers, because "the output" means two different things to a user:
      settlements — the ranked consequence table, what run_pipeline exports
      inundation  — the flood extent itself, as depth-class polygons
    A GIS user asking for the shapefile of a dam-break study wants the second
    at least as often as the first.

    Split out of the run so it can be exercised, and back-filled onto an
    existing run directory, without re-solving 24 h of shallow water.

    Every export carries `provenance`, which `export_shp` archives into the
    zip: this run PRESCRIBES its release, so a shapefile that escapes into a
    GIS with no caveat attached would read as a modelled dam-break result.
    """
    from src.m8_outputs.exporters import export_shp, export_kml, export_cap_json
    import geopandas as _gpd
    import run_pipeline as _rp

    exports_dir = out / "exports"
    safe_name = "Annamayya_Dam_Cheyyeru_routed_release"
    prov = {
        "run_id": out.name,
        "run_type": "FORCED_HYDROGRAPH_INUNDATION",
        "impoundment_modelled": False,
        "caveat": ("The release is PRESCRIBED from sourced figures and routed. "
                   "Discharge is an input to this run, not a measured output, and "
                   "no impoundment is drained through a breach here."),
        "t0_ist": T0_IST,
        "sources": {k: v["source"] for k, v in SOURCES.items()},
        "released_volume_m3": released_volume_m3,
        "peak_q_m3s": peak_q_m3s,
        "flood_threshold_m": 0.3,
    }

    vil_wgs = vil.to_crs("EPSG:4326") if vil.crs is not None else vil
    shp_path = export_shp(vil_wgs, exports_dir, safe_name + "_settlements", provenance=prov)
    kml_path = export_kml(vil_wgs, exports_dir, safe_name + "_settlements", provenance=prov)

    # The extent, vectorised with the same depth classes the map already uses,
    # so the exported polygons and the on-screen flood are the same object.
    extent_fc = _rp._depth_to_geojson(max_depth_grid, transform, t_s, to_wgs84)
    extent_paths: dict[str, str] = {}
    if extent_fc["features"]:
        extent = _gpd.GeoDataFrame.from_features(extent_fc["features"], crs="EPSG:4326")
        extent["run_id"] = out.name
        # export_kml colours by priority_score; depth class IS the ranking here.
        extent["priority_score"] = extent["depth_class"].astype(float)
        extent_paths = {
            "shp": str(export_shp(extent, exports_dir, safe_name + "_inundation",
                                  provenance=prov)),
            "kml": str(export_kml(extent, exports_dir, safe_name + "_inundation",
                                  provenance=prov)),
        }
        print(f"exports: inundation extent, {len(extent)} depth-class polygons")
    else:
        print("exports: no cell over the display threshold — extent layer skipped")

    cap_path = export_cap_json(
        vil_wgs, "Annamayya Dam (Cheyyeru) - routed release",
        {"method": "FORCED_HYDROGRAPH_INUNDATION (release prescribed, not modelled)",
         "Q_p_m3s": round(peak_q_m3s, 1),
         "t_f_h": None},
        exports_dir,
    )
    print(f"exports: {shp_path.name}, {kml_path.name}, "
          f"{Path(cap_path).name} -> {exports_dir}")
    return shp_path, kml_path, extent_paths, cap_path


def run_stage1(a) -> None:
    """Stage 1 -- route the sourced Pincha surge to the handoff section.

    This is a FORCED ROUTING RUN. No Pincha dam geometry is invented for it: the
    release is the sourced surge hydrograph, injected at the ring-bund
    coordinate, and what Stage 1 reports is the discharge that ARRIVES.
    """
    from src.m3_breach.cascade import generate_pincha_outflow

    dem, tr, crs, dx, dy, repaired = build_grid(a.coarsen)
    print("DEM %s at %.1f m (%d cells conditioned); min bed %.2f m, cells below 60 m: %d"
          % (dem.shape, dx, int(repaired.sum()), dem.min(), int((dem < 60).sum())))

    handoff = locate_handoff(dem, tr, crs)
    print("\nHANDOFF SECTION (measured, not assumed)")
    print("  reservoir 192.50 m plane starts at s = %.2f km (%.5f/%.5f)"
          % (handoff["plane_first_cell"]["s_km"], handoff["plane_first_cell"]["lat"],
             handoff["plane_first_cell"]["lon"]))
    print("  last real bed upstream of it: s = %.2f km, %.5f/%.5f, bed %.2f m"
          % (handoff["s_along_reach_km"], handoff["lat"], handoff["lon"], handoff["bed_m"]))
    print("  that is %.2f km UPSTREAM of the dam along the mapped reach"
          % handoff["distance_to_dam_along_reach_km"])
    print("  section: %d cells at %.1f m spacing"
          % (len(handoff["section_cells"]), handoff["section_spacing_m"]))
    print("  mapped reach is %.2f km and its head sits %.2f km from the Pincha ring bund"
          % (handoff["reach_length_km"], handoff["reach_head_to_pincha_km"]))
    print("  (that is the Cheyyeru's head at Rayavaram, where Pincha and Bahuda meet "
          "-- NOT an unmapped gap: nearest mapped river of any name to the ring bund "
          "is %.2f km)" % handoff["nearest_mapped_river_to_pincha_km"])

    from pyproj import Transformer
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    bx, by = fwd.transform(PINCHA_LON, PINCHA_LAT)
    iy, ix = rasterio.transform.rowcol(tr, bx, by)
    w = dem[max(0, iy - 4):iy + 5, max(0, ix - 4):ix + 5]
    o = np.unravel_index(np.argmin(w), w.shape)
    iy, ix = max(0, iy - 4) + o[0], max(0, ix - 4) + o[1]
    print("\nrelease cell row=%d col=%d bed %.2f m (Pincha ring bund, %s/%s)"
          % (iy, ix, dem[iy, ix], PINCHA_LAT, PINCHA_LON))

    # Event clock: Pincha fails at lead_time_s BEFORE T=0. Stage 1's own t=0 is
    # that failure, so t_s = t_stage1 - lead_time_s.
    up = SCENARIOS["annamayya"]["cascade"]["upstream"]
    lead_s = float(up["lead_time_s"])
    t = np.arange(0.0, a.cap_hours * 3600.0 + 300.0, 300.0)
    q = generate_pincha_outflow(
        t, t_failure_s=0.0,
        peak_q_m3s=float(SOURCES["pincha_surge_peak_m3s"]["value"]),
        base_q_m3s=float(up["base_q_m3s"]),
        duration_s=float(up["duration_s"]))
    vol = _trapz(q, t)
    print("Pincha surge: peak %.1f m3/s, base %.1f, pulse %.0f min, total %.2f MCM over %.0f h"
          % (q.max(), float(up["base_q_m3s"]), float(up["duration_s"]) / 60.0,
             vol / 1e6, a.cap_hours))
    print("  (sourced Pincha storage 9.28 MCM gross / 14.0 MCM surcharge -- the "
          "surplus is the base flow the catchment kept delivering)")

    out = ROOT / a.out
    out.mkdir(parents=True, exist_ok=True)
    _t_wall = time.time()

    def _progress(t_now, t_total):
        el = time.time() - _t_wall
        frac = max(t_now / max(t_total, 1e-9), 1e-9)
        print("    stage1 %5.2f h / %.1f h | wall %5.1f min | projected %6.1f min"
              % (t_now / 3600.0, t_total / 3600.0, el / 60.0, el / frac / 60.0), flush=True)

    res = run_2d_swe_simulation(
        progress_cb=_progress,
        elevation_grid=dem, dx_m=dx, dy_m=dy,
        inflow_x_idx=ix, inflow_y_idx=iy,
        hydrograph_t_s=t, hydrograph_Q_m3s=q,
        total_duration_s=a.cap_hours * 3600.0,
        save_interval_s=float(a.save_interval_s),
        manning_n=_roughness_field(dem, tr, crs, a),
        scenario_name="annamayya_stage1",
        stop_when_quiescent=False,
    )
    mc = res.mass_closure()
    print("\nstage 1 ended at t=%.2f h; mass closure %.4f%%  outflow %.3e m3"
          % (res.end_time_s / 3600.0, mc["relative_error"] * 100.0, mc["outflow_m3"]))

    sec = section_discharge(res, handoff)
    qa = np.array(sec["Q_m3s"])
    ta = np.array(sec["t_s"])
    wetw = np.array(sec["wetted_width_m"])
    arrival_stage1_s = float(ta[wetw > 0][0]) if (wetw > 0).any() else None
    i_pk = int(np.argmax(qa)) if qa.size else 0

    print("\n" + "=" * 70)
    print("STAGE 1 RESULT -- discharge MEASURED at the handoff section")
    print("=" * 70)
    if arrival_stage1_s is None:
        print("  THE FRONT NEVER REACHED THE SECTION within the run window.")
    else:
        t_s_arr = arrival_stage1_s - lead_s
        print("  arrival at the section : t_stage1 %7.1f min -> event clock t_s = %+9.1f s (%+.1f min)"
              % (arrival_stage1_s / 60.0, t_s_arr, t_s_arr / 60.0))
        print("  peak Q at the section  : %.1f m3/s at t_stage1 %.1f min (t_s %+.0f s)"
              % (qa.max(), ta[i_pk] / 60.0, ta[i_pk] - lead_s))
        print("  peak depth / speed     : %.2f m / %.2f m/s"
              % (max(sec["h_max_m"]), max(sec["speed_max_ms"])))
        print("  peak wetted width      : %.0f m over %d section cells (%.0f m long)"
              % (max(sec["wetted_width_m"]), len(handoff["section_cells"]),
                 len(handoff["section_cells"]) * handoff["section_spacing_m"]))
        print("  volume through section : %.2f MCM of %.2f MCM released"
              % (_trapz(qa, ta) / 1e6, vol / 1e6))

    arm = SOURCES["peak_total_inflow_annamayya_m3s"]
    runoff_pk = float(SCENARIOS["annamayya"]["cascade"]["catchment_runoff"]["peak_m3s"])
    got = (qa.max() if qa.size else 0.0) + runoff_pk
    print("\n  AGAINST THE THREE AGENCY ARMS (peak TOTAL inflow at Annamayya):")
    print("    routed Pincha peak at the section   %10.1f m3/s" % (qa.max() if qa.size else 0.0))
    print("    + sourced catchment runoff peak     %10.1f m3/s" % runoff_pk)
    print("    = upper bound on total inflow       %10.1f m3/s" % got)
    for k, label in (("mha_official", "MHA"), ("cwc_appraisal", "CWC"),
                     ("iisc_reconstruction", "IISc upper arm")):
        tgt = float(arm[k])
        verdict = ("MET" if got >= tgt else
                   "SHORT by %.1f m3/s (%.1f %%)" % (tgt - got, 100.0 * (tgt - got) / tgt))
        print("    vs %-16s %10.1f m3/s   %s" % (label, tgt, verdict))

    payload = {
        "stage": 1,
        "run_type": "FORCED_ROUTING_TO_HANDOFF",
        "not_a_pincha_dam_break": True,
        "why": ("Stage 1's only job is to determine the water arriving at "
                "Annamayya. No Pincha dam geometry is invented for it: the "
                "release is the sourced surge hydrograph injected at the ring "
                "bund coordinate, and Q at the handoff is MEASURED from the "
                "solver's h, u, v -- never prescribed."),
        "t0_ist": T0_IST,
        "event_clock_note": ("t_s is on the EVENT clock: t_s = 0 is the 06:30 IST "
                             "Annamayya washout. Pincha fails at t_s = %+.0f s." % -lead_s),
        "pincha_lead_time_s": -lead_s,
        "forcing": {
            "peak_q_m3s": float(q.max()),
            "base_q_m3s": float(up["base_q_m3s"]),
            "duration_s": float(up["duration_s"]),
            "released_volume_m3": float(vol),
            "source": SOURCES["pincha_surge_peak_m3s"],
        },
        "handoff_section": handoff,
        "arrival_t_stage1_s": arrival_stage1_s,
        "arrival_t_s": (arrival_stage1_s - lead_s) if arrival_stage1_s is not None else None,
        "measured": sec,
        "peak_Q_m3s": float(qa.max()) if qa.size else None,
        "volume_through_section_m3": float(_trapz(qa, ta)) if qa.size else None,
        "mass_closure_relative_error": float(mc["relative_error"]),
        "classification": {
            "Q_m3s": "MEASURED -- control-section flux of the solver's own state",
            "h_m": "MEASURED",
            "u_ms": "MEASURED",
            "v_ms": "MEASURED",
            "wse_m": "MEASURED -- bed + depth, on real bed upstream of the water plane",
            "wetted_width_m": ("MEASURED at grid resolution; a %.0f m cell cannot "
                               "resolve a narrower channel" % dx),
            "flow_area_m2": "MEASURED at grid resolution",
            "anything_inside_the_water_plane": (
                "NOT_COMPUTED -- the DEM under the reservoir is a flat 192.50 m DSM "
                "water surface, not bed. Nothing downstream of handoff_section is "
                "derived here."),
        },
        "grid": {"shape": list(dem.shape), "dx_m": float(dx), "dy_m": float(dy),
                 "coarsen": int(a.coarsen), "min_bed_m": float(dem.min()),
                 "cells_below_60m": int((dem < 60).sum())},
    }
    (out / "handoff.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print("\nwrote %s" % (out / "handoff.json"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coarsen", type=int, default=5)
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--cap-hours", type=float, default=8.0,
                    help="safety cap; the run stops earlier when the flood stops")
    ap.add_argument("--out", default="data/scenarios/annamayya_routed")
    ap.add_argument("--stage", type=int, choices=(1, 2), default=2,
                    help="1: route the Pincha surge to the handoff section and "
                         "export Q(t). 2: the downstream inundation run.")
    ap.add_argument("--channel-roughness", action="store_true",
                    help="give the MAPPED channel n=0.035 (CHANNEL_MANNING_N) "
                         "instead of the uniform floodplain value. "
                         "apply_channel_roughness measured the channel n as the "
                         "lever that moves the front (4.26 km -> 1.70 km for "
                         "n 0.020 -> 0.100) while floodplain n over 0.035-0.150 "
                         "moved it not at all -- an undifferentiated grid pulls "
                         "on the wrong one.")
    ap.add_argument("--condition-flowline", action="store_true",
                    help="make the mapped river hydraulically continuous, as "
                         "run_pipeline does. A 30 m DSM renders a gorge floor as "
                         "a staircase of disconnected bowls and the flood must "
                         "fill each one before advancing.")
    ap.add_argument("--open-outlets", action="store_true",
                    help="open the no-data wall where the mapped river leaves the "
                         "data footprint, as run_pipeline does. Without it this "
                         "domain is SEALED (measured outflow 0.000e+00) and every "
                         "extent is an upper bound on ponding.")
    ap.add_argument("--spinup-hours", type=float, default=0.0,
                    help="fill the channel with the SOURCED pre-event baseflow "
                         "before the dam release, and use the result as the "
                         "initial condition. 0 = start from a dry bed.")
    ap.add_argument("--manning", type=float, default=0.045,
                    help="floodplain Manning n (the uniform value when "
                         "--channel-roughness is off)")
    ap.add_argument("--save-interval-s", type=float, default=900.0,
                    help="state save cadence; stage 1 needs it fine enough to "
                         "resolve an arrival time")
    a = ap.parse_args()

    if a.stage == 1:
        run_stage1(a)
        return

    # One grid for both stages -- see build_grid. A measured floor, not a
    # statistic: at coarsen 5 this DEM's 0.1 percentile IS 0.00 m, because 216
    # zero-fringe cells are themselves in the array, so the old floor landed at
    # -100 m and walled none of them. Excluding the zeros lifts the percentile
    # to 64.79 m, but -100 m of margin still puts the floor at -35.21 m and
    # leaves 97 cells below 60 m. See dem_floor_m in SCENARIOS for how 60.0 was
    # measured and why thalweg_m - 100 is NOT it.
    dem, tr, crs, dx, dy, repaired = build_grid(a.coarsen)
    print(f"DEM {dem.shape} at {dx:.1f} m  ({repaired.sum()} cells conditioned), "
          f"min bed {dem.min():.2f} m, cells below 60 m: {int((dem < 60).sum())}")

    from pyproj import Transformer
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    bx, by = fwd.transform(BREACH_LON, BREACH_LAT)
    ix = int((bx - tr.c) / tr.a); iy = int((by - tr.f) / tr.e)
    # put the release on the local low point so it enters the channel
    w = dem[max(0, iy - 4):iy + 5, max(0, ix - 4):ix + 5]
    o = np.unravel_index(np.argmin(w), w.shape)
    iy, ix = max(0, iy - 4) + o[0], max(0, ix - 4) + o[1]
    print(f"release cell row={iy} col={ix}  bed {dem[iy, ix]:.2f} m")
    a._ix, a._iy = ix, iy

    dem, channel_mask, terrain_reports = _condition_terrain(
        dem, repaired, tr, crs, dem[iy, ix], a)
    a._channel_mask = channel_mask
    manning = _roughness_field(dem, tr, crs, a)
    h0 = _spinup(dem, dx, dy, manning, a)

    t, q, vol = build_hydrograph(a.hours)
    # The solver samples the hydrograph with np.interp, which CLAMPS to the last
    # value outside the table -- so a run capped longer than the hydrograph would
    # hold the final discharge for every remaining hour and inject water no source
    # authorises. At --hours 8 --cap-hours 24 that is 1,227 m3/s for 16 h = 70.7 MCM,
    # 1.61x the sourced release, with mass closure still reading perfect because the
    # water is conserved -- it is fabricated at the source. Pad with zeros instead:
    # the release ended.
    if a.cap_hours > a.hours:
        t = np.concatenate([t, [a.hours * 3600.0 + 1.0, a.cap_hours * 3600.0]])
        q = np.concatenate([q, [0.0, 0.0]])
    print(f"hydrograph: peak {q.max():,.0f} m3/s, total {vol/1e6:,.1f} MCM "
          f"over {a.hours:.0f} h "
          f"(storage {SOURCES['gross_storage_mcm']['value']:.2f} MCM + continuing inflow)"
          f"{f', zero after {a.hours:.0f} h to a {a.cap_hours:.0f} h cap' if a.cap_hours > a.hours else ''}")

    out = ROOT / a.out
    (out / "depth_rasters").mkdir(parents=True, exist_ok=True)

    _t_wall = time.time()

    def _progress(t_now, t_total):
        el = time.time() - _t_wall
        frac = max(t_now / max(t_total, 1e-9), 1e-9)
        print(f"    sim {t_now/3600:5.2f} h / {t_total/3600:.1f} h "
              f"| wall {el/60:5.1f} min | projected total {el/frac/60:6.1f} min",
              flush=True)

    res = run_2d_swe_simulation(
        progress_cb=_progress,
        elevation_grid=dem, dx_m=dx, dy_m=dy,
        inflow_x_idx=ix, inflow_y_idx=iy,
        hydrograph_t_s=t, hydrograph_Q_m3s=q,
        total_duration_s=a.cap_hours * 3600.0,
        save_interval_s=float(a.save_interval_s),
        manning_n=manning,
        initial_depth=h0,
        scenario_name="annamayya_routed",
        stop_when_quiescent=True,
        quiescent_hold_s=1800.0,
    )
    print(f"\nended at t={res.end_time_s/3600:.2f} h "
          f"({'flood quiescent' if res.stopped_early else 'hit the cap'})")

    mc = res.mass_closure()
    print(f"mass: in {mc['initial_m3']+mc['injected_m3']:.3e} m3, "
          f"stored {mc['stored_m3']:.3e}, outflow {mc['outflow_m3']:.3e}, "
          f"closure {mc['relative_error']*100:.4f}%")
    print(f"gross outflow across the outer faces: {res.volume_outflow_gross_m3:.3e} m3")

    # UI artifacts ---------------------------------------------------------
    # The web UI reads a run through its manifest: GeoJSON depth-class polygons
    # and an RGBA preview per frame, a results layer, a hydrograph, and hashed
    # artifact entries. Rasters alone are enough for the animation and not
    # enough for the map, which is why the first version of this run could not
    # be opened in the browser at all.
    from pyproj import Transformer as _Tr
    import run_pipeline as _rp
    from src.run_manifest import (fingerprint, new_manifest, register_artifact,
                                  transition, write_manifest, is_valid)

    to_wgs84 = _Tr.from_crs(crs, "EPSG:4326", always_xy=True).transform

    prof = {"driver": "GTiff", "height": dem.shape[0], "width": dem.shape[1],
            "count": 1, "dtype": "float32", "crs": crs, "transform": tr,
            "nodata": -9999.0, "compress": "lzw"}
    for i, (ts, h) in enumerate(zip(res.times_s, res.depth_grids)):
        with rasterio.open(out / "depth_rasters" / f"depth_{i:03d}.tif", "w", **prof) as d:
            d.write(h.astype("float32"), 1)
    with rasterio.open(out / "max_depth.tif", "w", **prof) as d:
        d.write(res.max_depth_grid.astype("float32"), 1)

    wet = res.max_depth_grid > 0.30

    snaps = out / "snapshots"; snaps.mkdir(exist_ok=True)
    prev = out / "preview_rasters"; prev.mkdir(exist_ok=True)
    idx = []
    for i, (ts, dg) in enumerate(zip(res.times_s, res.depth_grids)):
        fp = snaps / f"frame_{i:03d}.geojson"
        pp = prev / f"frame_{i:03d}.png"
        fp.write_text(json.dumps(_rp._depth_to_geojson(dg, tr, ts, to_wgs84)),
                      encoding="utf-8")
        bounds = _rp._write_depth_preview(dg, pp, tr, to_wgs84)
        idx.append({"frame_idx": i, "t_s": float(ts), "t_min": round(ts / 60.0, 1),
                    "stage": "routing",
                    "phase_title": ("Release begins (T+0 min)" if i == 0
                                    else f"Flood routing (T+{ts/60:.0f} min)"),
                    "path": str(fp), "preview_path": str(pp),
                    "preview_bounds": bounds,
                    "provenance": "FORCED_HYDROGRAPH_INUNDATION"})
    (out / "snapshots_index.json").write_text(json.dumps(idx, indent=1), encoding="utf-8")
    print(f"wrote {len(idx)} GeoJSON frames + preview rasters")

    # results layer: settlements with the depth the routed flood puts on them
    import geopandas as gpd
    vil = gpd.read_file(ROOT / "data" / "admin" / "annamayya_villages.geojson").to_crs(crs)
    # Over each village POLYGON, not the single cell under its centroid. See
    # village_exposure()'s docstring for what the centroid sample cost: 11 of 23
    # settlements read NOT REACHED with part of them under water, and 30,571
    # people went unscored.
    _exp = village_exposure(vil, res.max_depth_grid, res.arrival_time_s_grid, tr)
    depths = _exp["max_depth_m"]
    arr_min = _exp["water_arrival_min"]
    pop_risk = _exp["pop_at_risk"]
    par = int(sum(pop_risk))

    # The UI's exposure panel reads named fields. Writing only depth left it
    # rendering "0 people / 0 buildings", which reads as "nobody was affected"
    # when the truth is "that number was never computed". Every field below is
    # either computed from this run or explicitly null with a reason -- a null
    # is a status, a zero is a claim.
    vil["max_depth_m"] = depths
    vil["mean_depth_m"] = _exp["mean_depth_m"]
    vil["flooded_area_frac"] = _exp["flooded_area_frac"]
    vil["inundated"] = _exp["inundated"]
    vil["water_arrival_min"] = arr_min
    vil["pop_at_risk"] = pop_risk
    vil["exposure_status"] = _exp["exposure_status"]
    vil["pop_provenance"] = ("PROXY: OSM place population carried on "
                         "annamayya_villages.geojson, apportioned by flooded "
                         "area fraction (uniform density assumed), matching "
                         "m5_exposure.compute_village_exposure")
    vil["provenance"] = "FORCED_HYDROGRAPH_INUNDATION"
    vil["label"] = vil["village_name"]

    # Not computed by this run, and said so rather than reported as zero.
    _nc = "NOT_COMPUTED: no OSM building layer exists for this AOI"
    vil["buildings_flooded"] = None
    vil["buildings_provenance"] = _nc
    vil["loss_inr"] = None
    vil["loss_provenance"] = "NOT_COMPUTED: no loss model run for a routed inundation"
    vil["hospitals_flooded"] = None
    vil["schools_flooded"] = None
    vil["isolation_computed"] = False
    vil["isolation_time_min"] = None
    vil["evacuation_window_min"] = arr_min          # warning time == arrival time here
    vil["road_nodes_found"] = 0

    # Priority: who to reach first. Ranked on what this run actually measured --
    # people exposed, then how fast the water arrives, then how deep it gets.
    order = sorted(range(len(vil)),
                   key=lambda i: (-pop_risk[i],
                                  arr_min[i] if arr_min[i] is not None else 1e9,
                                  -depths[i]))
    rank = [0] * len(vil)
    for place, i in enumerate(order, start=1):
        rank[i] = place
    mx = max(1.0, max(depths))
    vil["priority_rank"] = rank
    vil["priority_score"] = [
        round((pop_risk[i] / max(1, max(pop_risk))) * 0.6 + (depths[i] / mx) * 0.4, 3)
        for i in range(len(vil))]
    vil["priority_method"] = "pop_at_risk 0.6 + normalised max depth 0.4"

    vil.to_crs("EPSG:4326").to_file(out / "results.geojson", driver="GeoJSON")
    n_hit = int(sum(vil["inundated"]))
    n_arr = sum(1 for a_ in arr_min if a_ is not None)
    print(f"results.geojson: {n_hit}/{len(vil)} settlements inundated, PAR {par:,}, "
          f"{n_arr} with a computed arrival time")

    (out / "hydrograph.json").write_text(json.dumps({
        "central": {"t_s": [float(x) for x in t], "Q_m3s": [float(x) for x in q],
                    "Q_p": float(q.max())},
        "measured": {"t_s": [float(x) for x in t], "Q_m3s": [float(x) for x in q],
                     "source": ("PRESCRIBED - this run routes a sourced release; "
                                "Q is an input here, not a measured output"),
                     "note": "FORCED_HYDROGRAPH_INUNDATION"},
    }, indent=1), encoding="utf-8")
    (out / "run_provenance.json").write_text(json.dumps({
        "run_type": "FORCED_HYDROGRAPH_INUNDATION",
        "not_a_reservoir_drainage_run": True,
        "why": ("Copernicus GLO-30 here is a DSM captured with the reservoir full: "
                "the DEM at the dam is a flat 192.50 m water plane against a published "
                "206.0 m crest, 740.23 km2 of the domain lies below that crest, and the "
                "valley is 25.1 km wide there. The impoundment cannot be emplaced or "
                "confined, so it is not drained through a breach here -- the released "
                "water is prescribed and routed."),
        "t0_ist": T0_IST,
        "sources": SOURCES,
        "released_volume_m3": vol,
        "peak_q_m3s": float(q.max()),
        "end_time_s": float(res.end_time_s),
        "stopped_early": bool(res.stopped_early),
        "outflow_m3": float(res.volume_outflow_m3),
        "outflow_gross_m3": float(res.volume_outflow_gross_m3),
        "flooded_km2_over_0p3m": float(wet.sum() * dx * dy / 1e6),
        "max_depth_m": float(np.nanmax(res.max_depth_grid)),
    }, indent=1), encoding="utf-8")

    # ── M8 exports: .shp / .kml ───────────────────────────────────────────────
    shp_path, kml_path, extent_paths, cap_path = write_exports(
        out, vil, res.max_depth_grid, tr, to_wgs84,
        released_volume_m3=float(vol), peak_q_m3s=float(q.max()),
        t_s=float(res.end_time_s))

    # manifest, so the UI can open this run ---------------------------------
    run_root = ROOT / "data" / "scenarios"
    man = new_manifest(
        "annamayya",
        request={"mode": "forced_hydrograph_route", "coarsen": a.coarsen,
                 "hours": a.hours, "cap_hours": a.cap_hours,
                 "dam_name": "Annamayya Dam (Cheyyeru) - routed release"},
        config={"spinup_hours": float(a.spinup_hours),
                "condition_flowline": bool(a.condition_flowline),
                "open_outlets": bool(a.open_outlets),
                "manning_n": ("channel 0.035 / floodplain %.3f" % a.manning)
                             if a.channel_roughness else a.manning,
                "channel_roughness": bool(a.channel_roughness),
                "save_interval_s": float(a.save_interval_s), "dx_m": float(dx)},
        model={"solver": "swe_2d well-balanced MUSCL SSP-RK2",
               "run_type": "FORCED_HYDROGRAPH_INUNDATION"},
        inputs={"dem": "data/dem/annamayya_dem.tif",
                "sources": {k: v["source"] for k, v in SOURCES.items()}},
        run_id=out.name,
    )
    transition(man, "running")

    for nm, pth, mt in (("results", out / "results.geojson", "application/geo+json"),
                        ("max_depth", out / "max_depth.tif", "image/tiff"),
                        ("hydrograph", out / "hydrograph.json", "application/json"),
                        ("snapshots_index", out / "snapshots_index.json", "application/json")):
        register_artifact(man, pth, run_root=run_root, name=nm, media_type=mt,
                          required=True)

    # The exports are registered too, so the manifest vouches for them like any
    # other artifact and the download endpoint can serve them. Not `required`:
    # a missing optional export must not invalidate an otherwise good run.
    for nm, pth, mt in (
            ("export_settlements_shp", Path(str(shp_path)[:-4] + ".zip"), "application/zip"),
            ("export_settlements_kml", Path(kml_path), "application/vnd.google-earth.kml+xml"),
            ("export_inundation_shp", Path(extent_paths["shp"][:-4] + ".zip")
             if extent_paths else None, "application/zip"),
            ("export_inundation_kml", Path(extent_paths["kml"]) if extent_paths else None,
             "application/vnd.google-earth.kml+xml"),
            ("export_cap", Path(cap_path), "application/json")):
        if pth is not None and pth.exists():
            register_artifact(man, pth, run_root=run_root, name=nm, media_type=mt,
                              required=False)

    man["validity"] = {
        "valid": True,
        # No impoundment is modelled, so the barrier/pool verdict is not a check
        # this run can pass or fail. It says so rather than claiming a True.
        "geometry": "not_applicable",
        "run_type": "FORCED_HYDROGRAPH_INUNDATION",
        "impoundment_modelled": False,
        "physics": True,
        "sources": True,
        "required_artifacts": ["results", "max_depth", "hydrograph", "snapshots_index"],
        "mass_balance_relative_error": float(mc["relative_error"]),
        "reasons": [
            ("FORCED-HYDROGRAPH ROUTE, not a reservoir drainage. The release is "
             "PRESCRIBED from sourced figures and routed; discharge is an input "
             "here, not a measured output."),
            ("The impoundment is not modelled: Copernicus GLO-30 over this AOI is a "
             "DSM captured with the reservoir full (flat 192.50 m water plane against "
             "a published 206.0 m crest), so the dam and the valley floor beneath the "
             "pool are absent from the terrain."),
            (f"Mass closure {mc['relative_error']*100:.4f}%; outflow "
             f"{res.volume_outflow_m3:.3e} m3 - the domain is effectively sealed, so "
             f"the extent is an upper bound on ponding."),
        ],
    }
    man["metrics"] = {"total_par": par,
                      "total_buildings": None,
                      "buildings_provenance": _nc,
                      "flooded_km2": float(wet.sum() * dx * dy / 1e6),
                      "max_depth_m": float(np.nanmax(res.max_depth_grid)),
                      "settlements_inundated": n_hit}
    man["pipeline_result"] = {
        "results_geojson": str(out / "results.geojson"),
        "snapshots_index": str(out / "snapshots_index.json"),
        "hydrograph_json": str(out / "hydrograph.json"),
        "max_depth_tif": str(out / "max_depth.tif"),
        "cell_size_m": float(dx), "coarsen": a.coarsen,
    }
    transition(man, "completed")
    write_manifest(man, run_root)
    print(f"manifest written; is_valid -> {is_valid(man, run_root=run_root)}")

    print(f"\nflooded (>0.3 m): {wet.sum()*dx*dy/1e6:,.2f} km2   "
          f"max depth {np.nanmax(res.max_depth_grid):.2f} m")
    print(f"wrote {len(res.times_s)} frames -> {out}")


if __name__ == "__main__":
    main()
