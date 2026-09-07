"""
FloodSight — M4 Solver Validation
=================================
Benchmarks that the numerical solver must pass before its output is trusted.

Why this file exists
--------------------
``ritter.py`` provides the analytical dam-break solution, and the pipeline used
to write that analytical curve to ``ritter_validation.json`` and call it
validation. It was not: nothing compared the curve to the solver, and
``ritter.rmse`` was never called. A benchmark that never runs the thing under
test cannot fail, and a test that cannot fail is decoration.

These drivers run the actual solver and return a pass/fail with the error.

Benchmarks
----------
``run_ritter_benchmark``
    1D dam break on a flat, frictionless bed against the Ritter (1892)
    analytical solution. Tests shock/rarefaction structure and wave speed.

``run_lake_at_rest_benchmark``
    Still water over real terrain. A well-balanced scheme keeps it still; a
    scheme that computes the bed-slope source separately from the pressure flux
    generates spurious currents out of a flat lake. This is the benchmark the
    current first-order scheme is expected to FAIL until Phase 3 lands
    hydrostatic reconstruction — it is here to measure that, not to hide it.

``mass_balance``
    Volume accounting for any run: injected minus stored minus escaped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np

from .ritter import solve as ritter_solve, rmse as ritter_rmse
from .swe_2d import run_2d_swe_simulation, SimulationResult

logger = logging.getLogger(__name__)

_G = 9.81


@dataclass
class BenchmarkResult:
    """Outcome of one validation benchmark."""
    name:      str
    passed:    bool
    metric:    str            # what was measured, e.g. "RMSE depth [m]"
    value:     float
    tolerance: float
    detail:    dict           # series and diagnostics for plotting

    def summary(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        return (f"[{verdict}] {self.name}: {self.metric} = {self.value:.4f} "
                f"(tolerance {self.tolerance:.4f})")


# ──────────────────────────────────────────────────────────────────────────────
# Ritter dam break
# ──────────────────────────────────────────────────────────────────────────────

def run_ritter_benchmark(
    h1_m:        float = 10.0,
    t_eval_s:    float = 30.0,
    dx_m:        float = 5.0,
    domain_m:    float = 4000.0,
    tolerance_m: float = 0.60,
) -> BenchmarkResult:
    """
    Run the solver on a flat frictionless channel and compare against Ritter.

    The domain is 1D in substance: three rows with zero-gradient boundaries in
    y, so the y-fluxes cancel and the solution is a function of x alone.

    Parameters
    ----------
    h1_m        : upstream reservoir depth [m]
    t_eval_s    : time after breach at which to compare [s]
    dx_m        : cell size [m]
    domain_m    : total domain length [m], centred on the dam face
    tolerance_m : RMSE above which the benchmark fails [m]

    Notes
    -----
    The comparison is restricted to the region the analytical solution actually
    governs at ``t_eval_s`` (the rarefaction fan plus a margin), because the
    undisturbed far field is trivially correct and would flatter the RMSE.
    """
    nx = int(domain_m / dx_m)
    ny = 3                                    # minimum for the interior stencil

    z = np.zeros((ny, nx), dtype=np.float64)  # flat, frictionless bed

    # Initial condition: reservoir on the left half, dry bed on the right
    x_centres = (np.arange(nx) + 0.5) * dx_m - domain_m / 2.0
    h0 = np.zeros((ny, nx), dtype=np.float64)
    h0[:, x_centres < 0.0] = h1_m

    # No breach inflow — the water is already present as an initial condition
    zero_hydro_t = np.array([0.0, t_eval_s * 10.0])
    zero_hydro_Q = np.array([0.0, 0.0])

    res = run_2d_swe_simulation(
        elevation_grid=z,
        dx_m=dx_m,
        dy_m=dx_m,
        inflow_x_idx=nx // 2,
        inflow_y_idx=ny // 2,
        hydrograph_t_s=zero_hydro_t,
        hydrograph_Q_m3s=zero_hydro_Q,
        total_duration_s=t_eval_s,
        save_interval_s=t_eval_s,          # we only need the final state
        manning_n=0.0,                     # Ritter is frictionless
        scenario_name="ritter_benchmark",
        initial_depth=h0,
    )

    h_sim = res.depth_grids[-1][ny // 2, :]
    t_end = res.times_s[-1]

    exact = ritter_solve(h1=h1_m, t=t_end, x=x_centres)

    # Compare only where the analytical solution is non-trivial at t_end:
    # the rarefaction fan, plus a margin either side.
    c0 = float(np.sqrt(_G * h1_m))
    lo, hi = -c0 * t_end - 200.0, 2.0 * c0 * t_end + 200.0
    window = (x_centres >= lo) & (x_centres <= hi)

    err = ritter_rmse(h_sim[window], exact.h[window])

    # Front position, compared like with like: the analytical solution only
    # reaches the detection depth some way behind its own zero-depth tip
    # (h -> 0 continuously), so measuring the simulated 5 cm contour against
    # the theoretical tip at 2*c0*t would penalise any scheme by construction.
    # Both fronts are therefore taken at the same threshold.
    front_depth = 0.05
    wet = np.nonzero(h_sim > front_depth)[0]
    front_sim = float(x_centres[wet[-1]]) if wet.size else float("nan")

    wet_exact = np.nonzero(exact.h > front_depth)[0]
    front_exact = float(x_centres[wet_exact[-1]]) if wet_exact.size else float("nan")

    front_tip = 2.0 * c0 * t_end          # theoretical zero-depth tip
    front_rel_err = (abs(front_sim - front_exact) / front_exact
                     if front_exact else float("nan"))

    # RMSE is the classical criterion and is what `passed` reports. Front
    # position is tracked separately because a diffusive first-order scheme can
    # sit well inside the RMSE tolerance while placing the wave front badly —
    # and front position IS arrival time, the headline output of this project.
    if front_rel_err > 0.10:
        logger.warning(
            "  front position off by %.0f%% — first-order diffusion. "
            "Arrival times will read LATE. Tracked as front_relative_error.",
            front_rel_err * 100.0,
        )

    result = BenchmarkResult(
        name="Ritter 1892 dam break (flat, frictionless)",
        passed=bool(err <= tolerance_m),
        metric="RMSE depth [m]",
        value=float(err),
        tolerance=float(tolerance_m),
        detail={
            "x_m":            x_centres.tolist(),
            "h_simulated_m":  h_sim.tolist(),
            "h_analytical_m": exact.h.tolist(),
            "t_s":            float(t_end),
            "h1_m":           float(h1_m),
            "c0_ms":          c0,
            "front_depth_m":         front_depth,
            "front_simulated_m":     front_sim,
            "front_analytical_m":    float(front_exact),
            "front_theoretical_tip_m": float(front_tip),
            "front_error_m":         float(front_sim - front_exact),
            "front_relative_error":  float(front_rel_err),
            "compare_window_m":      [float(lo), float(hi)],
        },
    )
    logger.info(result.summary())
    logger.info("  wave front: simulated %.1f m vs analytical %.1f m",
                front_sim, front_exact)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Lake at rest (well-balancedness)
# ──────────────────────────────────────────────────────────────────────────────

def run_lake_at_rest_benchmark(
    elevation_grid: np.ndarray,
    dx_m:           float,
    dy_m:           float,
    water_level_m:  Optional[float] = None,
    duration_s:     float = 600.0,
    tolerance_ms:   float = 1e-3,
) -> BenchmarkResult:
    """
    Fill the terrain to a flat water level and check that nothing moves.

    A well-balanced scheme preserves the lake-at-rest state exactly: the
    bed-slope source term cancels the hydrostatic pressure gradient. A scheme
    that discretises them independently does not cancel, and the error appears
    as currents in standing water — worst on steep terrain, which is exactly
    where this project operates.

    Passes when the maximum velocity anywhere stays below ``tolerance_ms``.
    """
    z = np.asarray(elevation_grid, dtype=np.float64)
    if water_level_m is None:
        # A level that wets a meaningful part of the domain without covering it
        water_level_m = float(np.percentile(z, 40.0))

    h0 = np.maximum(water_level_m - z, 0.0)
    if not np.any(h0 > 0):
        raise ValueError("water_level_m wets no cells — pick a higher level")

    res = run_2d_swe_simulation(
        elevation_grid=z,
        dx_m=dx_m,
        dy_m=dy_m,
        inflow_x_idx=z.shape[1] // 2,
        inflow_y_idx=z.shape[0] // 2,
        hydrograph_t_s=np.array([0.0, duration_s * 10.0]),
        hydrograph_Q_m3s=np.array([0.0, 0.0]),
        total_duration_s=duration_s,
        save_interval_s=duration_s,
        manning_n=0.0,          # friction would mask the imbalance by damping it
        scenario_name="lake_at_rest",
        initial_depth=h0,
    )

    u_end = res.u_grids[-1]
    v_end = res.v_grids[-1]
    wet = res.depth_grids[-1] > 0.01
    speed = np.sqrt(u_end**2 + v_end**2)
    max_speed = float(speed[wet].max()) if wet.any() else 0.0

    # How far the free surface drifted from flat, where it should not have moved
    eta_end = res.depth_grids[-1] + z
    eta_err = float(np.abs(eta_end[wet] - water_level_m).max()) if wet.any() else 0.0

    result = BenchmarkResult(
        name="Lake at rest (well-balancedness)",
        passed=bool(max_speed <= tolerance_ms),
        metric="max spurious speed [m/s]",
        value=max_speed,
        tolerance=float(tolerance_ms),
        detail={
            "water_level_m":        float(water_level_m),
            "duration_s":           float(duration_s),
            "wet_cells":            int(wet.sum()),
            "max_surface_error_m":  eta_err,
        },
    )
    logger.info(result.summary())
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Mass balance
# ──────────────────────────────────────────────────────────────────────────────

def mass_balance(
    res:              SimulationResult,
    hydrograph_t_s:   np.ndarray,
    hydrograph_Q_m3s: np.ndarray,
    initial_volume_m3: float = 0.0,
) -> dict:
    """
    Volume accounting for a completed run.

    Returns injected, stored and unaccounted volumes plus a relative closure
    error. The unaccounted term lumps together water that left through the open
    boundaries and water created or destroyed by the positivity clip — the
    solver does not yet separate them, so the number is reported as
    "outflow + numerical", not attributed.

    A rising closure error over successive runs is the signal that the
    positivity clip is manufacturing water.
    """
    t_end = res.times_s[-1] if res.times_s else 0.0
    t_grid = np.linspace(0.0, t_end, max(int(t_end), 2))
    Q = np.interp(t_grid, hydrograph_t_s, hydrograph_Q_m3s)
    injected = float(np.trapezoid(Q, t_grid)) if t_end > 0 else 0.0

    cell_area = res.dx_m * res.dy_m
    stored = float(res.depth_grids[-1].sum() * cell_area) if res.depth_grids else 0.0

    total_in = injected + initial_volume_m3
    unaccounted = total_in - stored
    rel_err = abs(unaccounted) / total_in if total_in > 0 else float("nan")

    return {
        "initial_volume_m3":  float(initial_volume_m3),
        "injected_volume_m3": injected,
        "stored_volume_m3":   stored,
        "unaccounted_m3":     float(unaccounted),
        "unaccounted_note":   "outflow through open boundaries + numerical clipping",
        "relative_error":     float(rel_err),
        "t_end_s":            float(t_end),
    }


def run_all(elevation_grid: np.ndarray | None = None,
            dx_m: float = 30.0, dy_m: float = 30.0) -> dict:
    """Run every benchmark and return a JSON-serialisable report."""
    report: dict = {"benchmarks": []}

    ritter = run_ritter_benchmark()
    report["benchmarks"].append(asdict(ritter))

    if elevation_grid is not None:
        lake = run_lake_at_rest_benchmark(elevation_grid, dx_m, dy_m)
        report["benchmarks"].append(asdict(lake))

    report["all_passed"] = all(b["passed"] for b in report["benchmarks"])
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    r = run_ritter_benchmark()
    print(r.summary())
