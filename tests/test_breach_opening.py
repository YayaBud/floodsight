"""P3 — the opening replaces the injection, and Q becomes an output.

Audit SS38 acceptance tests, run on controlled synthetic terrain rather than on
a scenario, because no scenario currently passes validity gate G4 (every one of
them is configured above what its own basin holds). Synthetic terrain is also
the stronger test for A1/A3/A5: the impounded volume, the head and the trigger
time are known exactly instead of being inferred from a DEM.

Geometry used throughout: a flat reservoir floor, a wall two cells thick, and a
lower apron downstream.

The two-cell wall is historical, not required. It was chosen because a one-cell
wall did not exist to a scheme whose interface bed was the MEAN of its two
cells -- it had no face anywhere at its own crest. Since the Audusse
reconstruction landed (2026-09-13) a one-cell wall holds exactly, pinned by
`tests/test_barrier_integrity.py`. The fixture is left at two cells so these
acceptance numbers stay comparable with the ones recorded in the audit.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.m4_solvers.swe_2d import BreachOpening, run_2d_swe_simulation

G = 9.81
DX = 25.0
NY, NX = 30, 90
WALL_C0, WALL_T = 40, 2          # wall occupies columns 40..41
FLOOR, CREST, APRON = 100.0, 160.0, 60.0


RIM = 170.0          # closes the reservoir; 10 m above the crest, no huge bed step


def _terrain():
    z = np.empty((NY, NX))
    z[:, :WALL_C0] = FLOOR
    z[:, WALL_C0:WALL_C0 + WALL_T] = CREST
    z[:, WALL_C0 + WALL_T:] = APRON
    # The domain edge is transmissive, so a reservoir that reaches it is not an
    # impoundment -- it drains off the grid and every measurement below becomes
    # meaningless. Wall the upstream end and both flanks; the ONLY way out is
    # the breach.
    z[:3, :WALL_C0] = RIM
    z[-3:, :WALL_C0] = RIM
    z[:, :3] = RIM
    return z


def _pool(z, level):
    h = np.zeros_like(z)
    up = np.zeros_like(z, dtype=bool)
    up[3:-3, 3:WALL_C0] = True
    h[up] = np.maximum(level - z[up], 0.0)
    return h, up


def _natural_bed(z):
    """The bed BEFORE the barrier was emplaced — what the pipeline passes as
    `z_natural`. Erosion stops here, so it must be the valley floor the
    structure stands on, not the structure itself."""
    zn = z.copy()
    zn[:, WALL_C0:WALL_C0 + WALL_T] = FLOOR
    return zn


def _opening(z_natural, *, width, formation_s, trigger_s=0.0, invert=FLOOR):
    """A breach across the middle 1/3 of the wall."""
    mask = np.zeros(z_natural.shape, dtype=bool)
    mask[NY // 3: 2 * NY // 3, WALL_C0:WALL_C0 + WALL_T] = True
    rows = np.indices(z_natural.shape)[0]
    axis_d = np.abs(rows - (NY // 2)) * DX          # along the wall
    return BreachOpening(
        mask=mask, z_natural=_natural_bed(z_natural), crest_elev_m=CREST,
        invert_final_m=invert, formation_s=formation_s, final_width_m=width,
        trigger_s=trigger_s, axis_distance_m=axis_d,
    )


def _run(z, h0, *, opening=None, cv=None, duration=600.0, save=60.0, manning=0.03):
    return run_2d_swe_simulation(
        elevation_grid=z.copy(), dx_m=DX, dy_m=DX,
        inflow_x_idx=10, inflow_y_idx=NY // 2,
        hydrograph_t_s=np.array([0.0, duration]),
        hydrograph_Q_m3s=np.array([0.0, 0.0]),
        total_duration_s=duration, save_interval_s=save,
        manning_n=manning, scenario_name="p3", initial_depth=h0,
        breach_opening=opening, upstream_cv_mask=cv,
    )


# ── A5: nothing happens before the trigger ────────────────────────────────────

def test_a5_domain_is_quiescent_and_the_pool_is_held_before_the_trigger():
    """As found, the injection started at t = 0 regardless of `t_trigger`."""
    z = _terrain()
    h0, up = _pool(z, 155.0)
    v0 = h0.sum() * DX * DX
    res = _run(z, h0, opening=_opening(z, width=200.0, formation_s=300.0,
                                       trigger_s=1e9), cv=up, duration=300.0)
    v1 = res.depth_grids[-1].sum() * DX * DX
    # Exact. This used to allow rel=5e-4 because the pool leaked ~0.05 % to the
    # scheme's wet/dry-front imbalance even with nothing happening. That defect
    # is gone (Audusse reconstruction, 2026-09-13) and the measured loss is now
    # 0.000e+00, so the tolerance is tightened to match -- a loose bound sized
    # for a fixed defect is a bound a real leak can hide inside.
    assert v1 == pytest.approx(v0, rel=1e-12)
    # The OPENING did nothing: no erosion, no re-attributed storage, and the
    # wall still stands at its crest everywhere.
    assert res.volume_bed_lowering_m3 == pytest.approx(0.0)
    assert res.elevation_grid[:, WALL_C0:WALL_C0 + WALL_T].min() == pytest.approx(CREST)
    assert max(res.breach_width_m) == 0.0, "the opening widened before its trigger"

    # NOTHING crosses an intact wall. This used to allow 1 % of the impoundment
    # because 6.9e4 m3 (0.23 %) seeped over the un-breached wall -- the
    # wet/dry-front imbalance, not the breach. Measured after the Audusse
    # reconstruction: 0.000000e+00 m3. An un-triggered breach behind an intact
    # barrier is the cleanest statement of the whole P2/P3 acceptance, so it is
    # asserted exactly.
    leaked = float(res.depth_grids[-1][:, WALL_C0 + WALL_T:].sum() * DX * DX)
    assert leaked == 0.0, (
        f"{leaked:.3e} m3 crossed an un-breached wall ({100 * leaked / v0:.4f} % "
        f"of the impoundment). Nothing should: the pool is 5 m below the crest "
        f"and the opening has not triggered."
    )


# ── A1: the pool is the only supply ───────────────────────────────────────────

def test_a1_total_water_equals_the_impoundment_no_double_count():
    """The C1 defect was `initial + injected = 2.00 x impounded`. With the
    opening there is nothing to inject: the impoundment IS the supply."""
    z = _terrain()
    h0, up = _pool(z, 155.0)
    impounded = h0.sum() * DX * DX
    res = _run(z, h0, opening=_opening(z, width=250.0, formation_s=120.0), cv=up)
    assert res.volume_injected_m3 == 0.0
    supplied = res.volume_initial_m3 + res.volume_injected_m3
    # The C1 ledger fact, exactly: the impoundment is supplied ONCE. This is
    # the number that read 2.00 on the certified archive run.
    assert supplied / impounded == pytest.approx(1.0, abs=1e-9)
    # and the ledger still closes once bed lowering is accounted separately
    mc = res.mass_closure()
    assert mc["relative_error"] < 0.01
    assert mc["bed_lowering_m3"] > 0.0


def test_injection_alongside_an_opening_is_refused():
    """Audit SS41: keeping both 'for comparison' reinstates the double count."""
    z = _terrain()
    h0, _ = _pool(z, 155.0)
    with pytest.raises(ValueError, match="2.00x|double count|Pass one or the other"):
        run_2d_swe_simulation(
            elevation_grid=z, dx_m=DX, dy_m=DX, inflow_x_idx=10, inflow_y_idx=NY // 2,
            hydrograph_t_s=np.array([0.0, 600.0]),
            hydrograph_Q_m3s=np.array([5000.0, 5000.0]),
            total_duration_s=60.0, save_interval_s=60.0, initial_depth=h0,
            breach_opening=_opening(z, width=250.0, formation_s=120.0),
        )


# ── the opening actually drains the pool ──────────────────────────────────────

def test_the_pool_drains_through_the_opening_and_only_through_it():
    z = _terrain()
    h0, up = _pool(z, 155.0)
    v0 = h0.sum() * DX * DX
    res = _run(z, h0, opening=_opening(z, width=250.0, formation_s=120.0),
               cv=up, duration=900.0)
    h1 = res.depth_grids[-1]
    downstream = h1[:, WALL_C0 + WALL_T:].sum() * DX * DX
    # The MECHANISM, not a rate. The opening must cut to the natural bed and
    # water must appear downstream of a wall that was previously sealed; the
    # rate itself is not asserted here -- A4 does that against the measured
    # discharge. Measured on this fixture after the Audusse reconstruction:
    # 1.9385e6 m3 downstream after 900 s from a 3.0525e7 m3 impoundment (it was
    # 1.59e6 while the barrier was also leaking everywhere else).
    op_mask = _opening(z, width=250.0, formation_s=120.0).mask
    assert res.elevation_grid[op_mask].min() == pytest.approx(FLOOR), (
        "the opening did not erode to the natural bed"
    )
    assert downstream > 0.01 * v0, "no water reached the downstream side at all"
    assert res.volume_bed_lowering_m3 > 0.0
    # the wall outside the opening still stands
    intact = np.zeros(z.shape, dtype=bool)
    intact[:NY // 3, WALL_C0:WALL_C0 + WALL_T] = True
    intact[2 * NY // 3:, WALL_C0:WALL_C0 + WALL_T] = True
    assert res.elevation_grid[intact].min() == pytest.approx(CREST), (
        "erosion escaped the breach mask"
    )


def test_erosion_never_cuts_below_the_natural_bed():
    z = _terrain()
    h0, up = _pool(z, 155.0)
    op = _opening(z, width=250.0, formation_s=60.0, invert=0.0)
    res = _run(z, h0, opening=op, cv=up, duration=600.0)
    # Only the opening is in question; the apron downstream is 60 m by design.
    assert res.elevation_grid[op.mask].min() >= FLOOR - 1e-9, (
        "the breach eroded below the valley floor the structure stands on"
    )
    assert np.array_equal(z, _terrain()), "the solver mutated the caller's DEM"


# ── A3: the jet speed must stay under free fall ───────────────────────────────

def test_a3_velocity_stays_below_the_free_fall_bound():
    """As found, the injection produced 67 m/s at coarsen 4 and 330 m/s at
    coarsen 2, with Froude to 17, because 17,169 m3/s was dropped into a 5x5
    kernel at the bottom of a 58 m pool. Draining through an opening cannot
    exceed sqrt(2 g H), the free-fall speed of the impounded column.

    The bound is the fall the water actually takes: pool surface (155 m) to the
    APRON, not pool surface to the reservoir FLOOR. The old `155.0 - FLOOR` form
    used the pool depth (55 m) where the real fall is 95 m, making the bound
    1.31x too tight -- and the test survived only because the fixture carried
    manning_n = 0.03. Friction can only REDUCE velocity, so a kinematic bound
    has to be checked with it off: at manning_n = 0 the old form measured
    34.73 m/s against its own 32.85 m/s and would have failed, while sitting
    comfortably under both the correct free-fall bound (43.17 m/s) and the
    Ritter dry-bed front speed 2*sqrt(g*h0) = 46.46 m/s.

    Measured 2026-09-13, audit Part VI SS55.7 and N-13.
    """
    z = _terrain()
    fall = 155.0 - APRON                     # 95 m, not the 55 m pool depth
    bound = np.sqrt(2 * G * fall)            # 43.17 m/s
    h0, up = _pool(z, 155.0)
    res = _run(z, h0, opening=_opening(z, width=250.0, formation_s=120.0),
               cv=up, duration=600.0, manning=0.0)
    worst = 0.0
    for h, u, v in zip(res.depth_grids, res.u_grids, res.v_grids):
        deep = h > 1.0                       # the audit's own criterion
        if deep.any():
            worst = max(worst, float(np.hypot(u[deep], v[deep]).max()))
    assert worst <= bound, (
        f"|v| = {worst:.1f} m/s exceeds the free-fall bound over the real fall "
        f"of {fall:.0f} m, sqrt(2g*fall) = {bound:.1f} m/s. Do NOT loosen this "
        f"bound -- water above free fall means energy is being manufactured."
    )


# ── A4: measured Q is a real signal ───────────────────────────────────────────

def test_a4_measured_q_is_produced_and_integrates_to_the_volume_released():
    z = _terrain()
    h0, up = _pool(z, 155.0)
    v0 = float(h0[up].sum() * DX * DX)
    res = _run(z, h0, opening=_opening(z, width=250.0, formation_s=120.0),
               cv=up, duration=900.0, save=900.0)
    assert res.breach_q_m3s, "no measured hydrograph was produced"
    q = np.asarray(res.breach_q_m3s); tt = np.asarray(res.breach_q_t_s)
    assert len(q) == len(tt) == len(res.breach_invert_m) == len(res.breach_width_m)
    assert q.max() > 0.0, "Q was measured as identically zero"
    # The invert and width series must be the shared kernel's, monotone the way
    # a breach grows: the invert falls from the crest, the width rises to B.
    inv = np.asarray(res.breach_invert_m); wid = np.asarray(res.breach_width_m)
    assert inv[0] >= inv[-1] and inv[-1] == pytest.approx(FLOOR)
    assert wid[0] <= wid[-1] and wid[-1] == pytest.approx(250.0)


def test_a4_measured_q_integrates_to_the_volume_that_left_the_pool():
    """A4. The integral closes to within 5 %, which is what makes the measured
    hydrograph a discharge rather than a diagnostic curve.

    Its instantaneous values are a different matter and must not be read as
    physics yet: the peak sample is 5.5e5 m3/s against a 900 s mean of
    3.8e4 m3/s, because storage-change/dt is dominated in the early steps by
    the wet/dry-front imbalance rather than by the opening. Against the SAME
    kernel's 0-D routed peak of 2.0e5 m3/s the ratio at peak is 2.7. Per the
    audit's SS41 that disagreement is a finding to report, not a growth law to
    retune -- and the evidence says it belongs to the solver's front treatment,
    not to the breach law.
    """
    z = _terrain()
    h0, up = _pool(z, 155.0)
    v0 = float(h0[up].sum() * DX * DX)
    res = _run(z, h0, opening=_opening(z, width=250.0, formation_s=120.0),
               cv=up, duration=900.0, save=900.0)
    q = np.asarray(res.breach_q_m3s); tt = np.asarray(res.breach_q_t_s)
    released = float(np.trapezoid(q, tt)) if hasattr(np, "trapezoid") else float(np.trapz(q, tt))
    remaining = float(res.depth_grids[-1][up].sum() * DX * DX)
    assert released == pytest.approx(v0 + res.volume_bed_lowering_m3 - remaining,
                                     rel=0.05)


# ── A7: the property that makes the solver worth keeping ──────────────────────

def test_a7_lake_at_rest_survives_a_mutable_bed():
    """`z` is now written inside the integration loop. The exact
    pressure-flux / bed-slope cancellation must still hold where nothing is
    eroding.

    CLEARED 2026-09-13. This was a strict xfail, and the reason it carried was
    correct at the time: the failure was never caused by the mutable bed, it
    was that a bounded lake did not stay at rest in this scheme at all --
    35.4 m of drift and 1.8 m/s in standing water. The Audusse hydrostatic
    reconstruction in `swe_2d._rhs` removed that, and A7 then passed on its
    own, which is exactly what the xfail was left here to detect. The mutable
    bed is now confirmed innocent by a test that passes rather than by one
    that is expected to fail."""
    z = _terrain()
    h0, up = _pool(z, 155.0)
    res = _run(z, h0, opening=None, cv=up, duration=300.0)
    h1 = res.depth_grids[-1]
    # Over the POOL. Cells elsewhere carry numerical dribbles of ~1e-6 m whose
    # bed is the 300 m rim or the 60 m apron; including them would measure the
    # terrain, not the free surface.
    pool = up & (h1 > 1e-3)
    eta = (z + h1)[pool]
    assert eta.max() - eta.min() < 1e-3, (
        f"lake at rest drifted by {eta.max() - eta.min():.2e} m"
    )
    assert float(np.hypot(res.u_grids[-1][pool], res.v_grids[-1][pool]).max()) < 1e-3
