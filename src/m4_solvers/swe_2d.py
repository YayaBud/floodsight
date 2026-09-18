"""
FloodSight — M4 Well-Balanced 2D Shallow Water Solver
=====================================================
2D Saint-Venant / shallow water equations, second order in space and time.

Scheme
------
- **Audusse hydrostatic reconstruction** — the bed-slope source is built into
  the interface fluxes rather than discretised separately, so the pressure
  gradient and the bed slope cancel *exactly* for water at rest.
- **MUSCL reconstruction with a minmod limiter** — second-order in space,
  reconstructing free surface elevation and primitive velocities.
- **SSP-RK2 (Heun)** — second-order in time, strong-stability-preserving.
- **Rusanov (local Lax-Friedrichs) flux** — robust across shocks and dry fronts.
- **Semi-implicit (point-implicit) friction** — unconditionally stable in thin
  films, so no velocity clamp is needed.
- **Kurganov-Petrova desingularisation** — velocity recovery that does not blow
  up as depth goes to zero.
- **Mass accounting** — injected, stored, boundary outflow and the positivity
  correction are all tracked, so closure error is reported rather than assumed.

Why this replaced the previous scheme
-------------------------------------
The earlier version computed the bed source as ``-g*h*dz/dx`` by central
differences while the flux carried ``g*h²/2``. Those do not cancel. Measured on
the real Phutkal terrain, standing water generated **20 m/s** spurious currents
— saturating the hard velocity clamp that existed to hide the instability — and
the free surface drifted **39 m** from flat in 10 minutes. Mass closure on a
conical bowl was **31.5%** off, because the unbalanced source drove depths
negative and ``h = max(h, 0)`` then destroyed the deficit.

Both failures came from the same missing property. The lake-at-rest and
mass-balance benchmarks in ``validation.py`` measure it directly.

References
----------
Audusse, E., Bouchut, F., Bristeau, M.-O., Klein, R., Perthame, B. (2004).
    A fast and stable well-balanced scheme with hydrostatic reconstruction for
    shallow water flows. SIAM J. Sci. Comput., 25(6), 2050-2065.
Kurganov, A., Petrova, G. (2007). A second-order well-balanced positivity
    preserving central-upwind scheme for the Saint-Venant system.
    Commun. Math. Sci., 5(1), 133-160.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Union

import numpy as np

from src.m3_breach.breach_kernel import breach_invert_at, breach_width_at

logger = logging.getLogger(__name__)

_G   = 9.81      # gravity [m/s²]
_EPS = 1e-3      # wet-depth threshold [m]
_DRY = 1e-8      # below this a cell is treated as fully dry

# ── Active window ────────────────────────────────────────────────────────────
# Measured on the Derna runs: at peak flood 1.1% of cells are wet and their
# bounding box is 6.4% of the domain. The other 94% is solved every step for
# nothing, because a dry cell contributes *exactly* zero -- with hL = hR = 0
# the Rusanov flux is identically zero and the bed-slope source carries
# h_bar = 0. Restricting the update to a box around the wet region is
# therefore not an approximation: it reproduces the full-domain result bit for
# bit, so long as no wet cell ever sits within the MUSCL stencil width of the
# box edge.
#
# The CFL condition bounds front motion at `cfl` cells per step (0.35), so a
# 6-cell halo stays ahead of the water for the 4 steps between rebuilds:
# 4 x 0.35 = 1.4 cells of travel, plus 2 cells of stencil, is 3.4 -- inside 6.
# A perimeter check every step catches any violation regardless, so the
# invariant does not silently depend on those constants staying put.
_HALO = 6
_WINDOW_EVERY = 4

# ponytail: escape hatch for A/B-ing the window against the full-domain solve.
# Set FLOODSIGHT_FULL_DOMAIN=1 to disable. Not a tuning knob -- the two paths
# must agree bit for bit, and tests/test_swe_active_window.py asserts they do.
_FULL_DOMAIN = bool(int(__import__("os").environ.get("FLOODSIGHT_FULL_DOMAIN", "0")))


def _active_window(h, seed_iy, seed_ix, halo=_HALO, prev=None):
    """Row/col slices of the box holding every wet cell, plus `halo`.

    Always contains the inflow point so the injection kernel is never clipped.

    ``prev`` is the previous window. Water cannot exist outside it -- that is
    the invariant the whole scheme rests on -- so the scan is confined to it
    rather than sweeping the full domain. At 28 m that scan was 5.6% of total
    runtime purely from reducing over 637k mostly-dry cells every rebuild.
    """
    if _FULL_DOMAIN:
        return slice(None), slice(None)
    ny, nx = h.shape
    oy = prev[0].start if prev is not None else 0
    ox = prev[1].start if prev is not None else 0
    sub = h[prev] if prev is not None else h
    wet = sub > _DRY
    rows = np.flatnonzero(np.any(wet, axis=1))
    cols = np.flatnonzero(np.any(wet, axis=0))
    rows = rows + oy
    cols = cols + ox
    if rows.size:
        i0, i1 = min(int(rows[0]), seed_iy), max(int(rows[-1]), seed_iy)
        j0, j1 = min(int(cols[0]), seed_ix), max(int(cols[-1]), seed_ix)
    else:
        i0 = i1 = seed_iy
        j0 = j1 = seed_ix
    return (slice(max(i0 - halo, 0), min(i1 + halo + 1, ny)),
            slice(max(j0 - halo, 0), min(j1 + halo + 1, nx)))


def _window_breached(h, win):
    """True if water has reached the window perimeter and it must be rebuilt.

    Cheap: touches only the border ring, not the interior. Cells clipped by the
    true domain edge do not count -- there the transmissive boundary is real.
    """
    if _FULL_DOMAIN:
        return False
    ny, nx = h.shape
    sy, sx = win
    b = h[sy, sx]
    if sy.start > 0 and bool(np.any(b[:2, :] > _DRY)):
        return True
    if sy.stop < ny and bool(np.any(b[-2:, :] > _DRY)):
        return True
    if sx.start > 0 and bool(np.any(b[:, :2] > _DRY)):
        return True
    if sx.stop < nx and bool(np.any(b[:, -2:] > _DRY)):
        return True
    return False

# ──────────────────────────────────────────────────────────────────────────────
# Optional numba acceleration
# ──────────────────────────────────────────────────────────────────────────────
# Profiling a 428x396 (169k cell) Rishiganga run: 120 s of simulated time cost
# 52 s of wall time, i.e. ~26 min for a 1 h simulation. Only 88k Python calls
# were made in that time, so this is not interpreter overhead -- it is numpy
# allocating a fresh temporary for every sub-expression, roughly fifteen of them
# per Rusanov flux evaluation, on arrays of 169k doubles.
#
# The three leaf kernels below are purely elementwise, so fusing each into a
# single explicit loop removes those temporaries entirely.
#
# The arithmetic is transcribed TERM FOR TERM from the numpy versions, in the
# same order, and fastmath is deliberately NOT enabled. The well-balanced
# property of this scheme is an exact floating-point cancellation -- the
# lake-at-rest benchmark asserts a spurious velocity of exactly 0.0 m/s -- and
# reassociating those operations would quietly destroy it. tests/
# test_swe_validation.py pins all five benchmark numbers and is the check on
# this claim.
#
# If numba is unavailable the numpy implementations are used unchanged.
#
# Tried and measured: adding parallel=True + prange to these four kernels
# (thread-parallelising each elementwise loop) was tested against the serial
# numba baseline on the Rishiganga benchmark (169,488 cells, coarsen 2). It made
# the solver SLOWER -- 11.5 s vs 10.1 s for 120 s of simulated time. Each kernel
# call operates on a modest array and _rhs invokes several of them per timestep,
# so the thread-pool dispatch/sync overhead per call outweighs the arithmetic
# saved. Reverted rather than kept as a theoretical win with a measured loss.
#
# The multicore win that DOES pay off is coarser-grained: the three breach arms
# already run as separate OS processes (see run_pipeline.py's
# ProcessPoolExecutor), which parallelises at a scale large enough to amortise
# its overhead.
try:
    from numba import njit as _njit
    _HAVE_NUMBA = True
except ImportError:                                   # pragma: no cover
    _HAVE_NUMBA = False

    def _njit(*a, **k):                               # type: ignore[misc]
        def wrap(fn):
            return fn
        return wrap



@dataclass
class InflowBoundary:
    """One inflow point for the multi-inflow interface (Part 3).

    Mirrors the scalar ``inflow_x_idx``/``inflow_y_idx``/``hydrograph_t_s``/
    ``hydrograph_Q_m3s``/``hydrograph_v_ms``/``inflow_direction`` parameters of
    ``run_2d_swe_simulation``, one instance per boundary, so a compound event
    with multiple breach/inflow locations can inject at each independently.
    """
    x_idx:             int
    y_idx:             int
    hydrograph_t_s:    np.ndarray
    hydrograph_Q_m3s:  np.ndarray
    hydrograph_v_ms:   np.ndarray | None = None
    direction:         tuple[float, float] | None = None


@dataclass
class BreachOpening:
    """A hole cut in the emplaced barrier, widening and deepening with time.

    This is what replaces the volumetric injection (audit defect C2). The
    injection put the breach hydrograph into the reservoir at its own deepest
    cell as a 5x5 Gaussian source -- so the discharge was an INPUT, the water
    had to be supplied a second time on top of the impoundment already in the
    domain (C1), and there was no structure for a breach to open (C3).

    Here the solver lowers the bed over the opening instead. The pool drains
    through it because its free surface is above the invert, and Q becomes an
    OUTPUT measured from the drop in upstream storage rather than a curve the
    caller hands in.

    `mask` is `breach_mask & barrier_mask` from `validate_geometry` -- both were
    already computed and both were thrown away. `z_natural` is the bed BEFORE
    the barrier was emplaced: erosion stops there, so a breach cannot cut below
    the valley floor the structure was built on.
    """
    mask:            np.ndarray            # bool, the cells the opening may occupy
    z_natural:       np.ndarray            # pre-emplacement bed; the erosion floor
    crest_elev_m:    float                 # invert starts here
    invert_final_m:  float                 # invert erodes to here
    formation_s:     float                 # t_f
    final_width_m:   float                 # B_final
    trigger_s:       float = 0.0           # nothing happens before this
    #: Distance of each cell from the breach centre measured ALONG the dam
    #: axis, in metres. `width(t)` is applied against this, so the opening
    #: grows sideways along the structure rather than radially. None falls back
    #: to plan distance from the mask's centroid, which is only right for a
    #: breach whose zone is already a narrow buffer on the axis.
    axis_distance_m: np.ndarray | None = None

    def state_at(self, t_s: float) -> tuple[float, float]:
        """`(invert_elev_m, width_m)` at time `t_s`, from the shared kernel."""
        elapsed = t_s - self.trigger_s
        if elapsed < 0.0:
            return self.crest_elev_m, 0.0
        return (
            breach_invert_at(elapsed, self.formation_s,
                             invert_start_m=self.crest_elev_m,
                             invert_final_m=self.invert_final_m),
            breach_width_at(elapsed, self.formation_s, self.final_width_m),
        )


@dataclass
class SimulationResult:
    times_s:             list[float]
    depth_grids:         list[np.ndarray]
    u_grids:             list[np.ndarray]
    v_grids:             list[np.ndarray]
    max_depth_grid:      np.ndarray
    arrival_time_s_grid: np.ndarray
    dx_m:                float
    dy_m:                float
    elevation_grid:      np.ndarray
    scenario_name:       str = ""
    #: Depth at t = 0 (the impoundment). `max_depth_grid` includes it; anything
    #: measuring CONSEQUENCE must subtract it -- see audit SS39 / P4.
    initial_depth_grid:  np.ndarray | None = None
    #: Volume bookkeeping, filled in by the integrator.
    volume_injected_m3:  float = 0.0
    volume_outflow_m3:   float = 0.0
    volume_clipped_m3:   float = 0.0
    volume_initial_m3:   float = 0.0
    #: Gross volume that actually crossed the outer faces OUTWARD. `volume_outflow_m3`
    #: is the NET and can be zero either because the domain is sealed or because
    #: inflow through a transmissive boundary cancelled the outflow; this one
    #: answers "can water leave at all".
    volume_outflow_gross_m3: float = 0.0
    #: Storage re-attributed by lowering the bed under standing water. When the
    #: breach erodes, the free surface is held and the DEPTH grows to match the
    #: new bed -- the water was always there, the container changed. Counted
    #: separately and never folded into `clipped`, which is manufactured mass.
    volume_bed_lowering_m3: float = 0.0
    #: Simulated time actually reached. Equals the requested duration unless the
    #: run stopped on quiescence, so consumers (timeline, animation) must read
    #: this rather than assuming the configured window.
    end_time_s:          float = 0.0
    #: True when the run ended because the flood stopped, not because it hit the cap.
    stopped_early:       bool = False
    #: Breach discharge MEASURED, not prescribed: t and Q sampled each save.
    breach_q_t_s:    list = field(default_factory=list)
    breach_q_m3s:    list = field(default_factory=list)
    breach_invert_m: list = field(default_factory=list)
    breach_width_m:  list = field(default_factory=list)

    def mass_closure(self) -> dict:
        """Volume in vs volume accounted for, as a relative error."""
        total_in = self.volume_initial_m3 + self.volume_injected_m3
        stored = float(self.depth_grids[-1].sum() * self.dx_m * self.dy_m) \
            if self.depth_grids else 0.0
        residual = (total_in + self.volume_bed_lowering_m3 - stored
                    - self.volume_outflow_m3 + self.volume_clipped_m3)
        return {
            "initial_m3":   self.volume_initial_m3,
            "injected_m3":  self.volume_injected_m3,
            "stored_m3":    stored,
            "outflow_m3":   self.volume_outflow_m3,
            "outflow_gross_m3": self.volume_outflow_gross_m3,
            "clipped_m3":   self.volume_clipped_m3,
            "bed_lowering_m3": self.volume_bed_lowering_m3,
            "residual_m3":  float(residual),
            "relative_error": float(abs(residual) / total_in) if total_in > 0 else 0.0,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Reconstruction helpers
# ──────────────────────────────────────────────────────────────────────────────

@_njit(cache=True, fastmath=False)
def _minmod_jit(a, b, out):
    for i in range(a.shape[0]):
        for j in range(a.shape[1]):
            av = a[i, j]
            bv = b[i, j]
            if av * bv > 0.0:
                aa = abs(av)
                ab = abs(bv)
                m = aa if aa < ab else ab
                out[i, j] = m if av > 0.0 else -m
            else:
                out[i, j] = 0.0
    return out


def _minmod(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Minmod limiter: the smaller slope when they agree in sign, else zero."""
    if _HAVE_NUMBA and a.ndim == 2:
        return _minmod_jit(a, b, np.empty(a.shape, dtype=np.float64))
    return np.where(a * b > 0.0,
                    np.sign(a) * np.minimum(np.abs(a), np.abs(b)),
                    0.0)


@_njit(cache=True, fastmath=False)
def _edge_values_jit(q, axis, qm, qp):
    n0, n1 = q.shape[0], q.shape[1]
    for i in range(n0):
        for j in range(n1):
            # Slope is zero in the first and last cell along `axis`, exactly as
            # the numpy version forces after rolling. Because those ends are
            # zeroed anyway, np.roll's wrap-around values never mattered and are
            # simply not computed here.
            if axis == 0:
                edge = (i == 0 or i == n0 - 1)
            else:
                edge = (j == 0 or j == n1 - 1)
            if edge:
                slope = 0.0
            else:
                if axis == 0:
                    back = q[i, j] - q[i - 1, j]
                    fwd = q[i + 1, j] - q[i, j]
                else:
                    back = q[i, j] - q[i, j - 1]
                    fwd = q[i, j + 1] - q[i, j]
                if back * fwd > 0.0:
                    ab, af = abs(back), abs(fwd)
                    m = ab if ab < af else af
                    slope = m if back > 0.0 else -m
                else:
                    slope = 0.0
            v = q[i, j]
            qm[i, j] = v - 0.5 * slope
            qp[i, j] = v + 0.5 * slope
    return qm, qp


def _edge_values(q: np.ndarray, axis: int) -> tuple[np.ndarray, np.ndarray]:
    """
    MUSCL-reconstructed values at the two cell edges along ``axis``.

    Returns ``(q_minus, q_plus)``: the value at the lower-index edge and at the
    higher-index edge of each cell. Linear reconstruction preserves the cell
    mean, which is what makes the well-balanced cancellation exact.
    """
    if _HAVE_NUMBA and q.ndim == 2:
        # Fuses the two np.roll copies, the minmod limiter and the two edge
        # reconstructions into one pass. np.roll allocates and copies the whole
        # array twice per call and this is called 1,248 times per 120 s of
        # simulated time.
        return _edge_values_jit(q, axis,
                                np.empty(q.shape, dtype=np.float64),
                                np.empty(q.shape, dtype=np.float64))

    back = q - np.roll(q, 1, axis=axis)
    fwd  = np.roll(q, -1, axis=axis) - q
    slope = _minmod(back, fwd)

    # No slope into the ghost region — first and last cells stay piecewise flat.
    idx_lo = [slice(None)] * q.ndim
    idx_hi = [slice(None)] * q.ndim
    idx_lo[axis] = 0
    idx_hi[axis] = -1
    slope[tuple(idx_lo)] = 0.0
    slope[tuple(idx_hi)] = 0.0

    return q - 0.5 * slope, q + 0.5 * slope


@_njit(cache=True, fastmath=False)
def _desingularise_jit(h, hq, out):
    eps2 = _EPS * _EPS
    for i in range(h.shape[0]):
        for j in range(h.shape[1]):
            hv = h[i, j]
            if hv > _DRY:
                h2 = hv * hv
                den = h2 if h2 > eps2 else eps2
                out[i, j] = 2.0 * hv * hq[i, j] / (h2 + den)
            else:
                out[i, j] = 0.0
    return out


def _desingularise(h: np.ndarray, hq: np.ndarray) -> np.ndarray:
    """
    Velocity from depth and discharge without blowing up as h → 0.

    Kurganov-Petrova: ``u = 2h·hq / (h² + max(h², eps²))``. For h well above
    eps this is exactly hq/h; below it, it rolls smoothly to zero instead of
    dividing by a vanishing number.
    """
    if _HAVE_NUMBA and h.ndim == 2:
        return _desingularise_jit(h, hq, np.empty(h.shape, dtype=np.float64))
    h2 = h * h
    return np.where(h > _DRY,
                    2.0 * h * hq / (h2 + np.maximum(h2, _EPS * _EPS)),
                    0.0)


@_njit(cache=True, fastmath=False)
def _rusanov_jit(hL, huL, hvL, uL, vL, hR, huR, hvR, uR, vR, normal_axis,
                 f_h, f_hu, f_hv, a_out):
    for i in range(hL.shape[0]):
        for j in range(hL.shape[1]):
            hl = hL[i, j]; hr = hR[i, j]
            hul = huL[i, j]; hur = huR[i, j]
            hvl = hvL[i, j]; hvr = hvR[i, j]
            ul = uL[i, j]; ur = uR[i, j]
            vl = vL[i, j]; vr = vR[i, j]

            cl = np.sqrt(_G * (hl if hl > 0.0 else 0.0))
            cr = np.sqrt(_G * (hr if hr > 0.0 else 0.0))

            if normal_axis == 0:
                sl = abs(ul) + cl
                sr = abs(ur) + cr
                a = sl if sl > sr else sr
                fhl = hul;  fhr = hur
                fhul = hul * ul + 0.5 * _G * hl * hl
                fhur = hur * ur + 0.5 * _G * hr * hr
                fhvl = hvl * ul
                fhvr = hvr * ur
            else:
                sl = abs(vl) + cl
                sr = abs(vr) + cr
                a = sl if sl > sr else sr
                fhl = hvl;  fhr = hvr
                fhul = hul * vl
                fhur = hur * vr
                fhvl = hvl * vl + 0.5 * _G * hl * hl
                fhvr = hvr * vr + 0.5 * _G * hr * hr

            f_h[i, j]  = 0.5 * (fhl + fhr)   - 0.5 * a * (hr - hl)
            f_hu[i, j] = 0.5 * (fhul + fhur) - 0.5 * a * (hur - hul)
            f_hv[i, j] = 0.5 * (fhvl + fhvr) - 0.5 * a * (hvr - hvl)
            a_out[i, j] = a
    return f_h, f_hu, f_hv, a_out


def _rusanov(hL, huL, hvL, uL, vL, hR, huR, hvR, uR, vR, normal_axis: int):
    """
    Rusanov (local Lax-Friedrichs) flux across an interface.

    ``normal_axis`` 0 means the interface normal is x, 1 means y.
    """
    if _HAVE_NUMBA and hL.ndim == 2:
        # No ascontiguousarray here: these are all slice views of the padded
        # grid, so forcing contiguity would copy ten full arrays per call --
        # reintroducing exactly the allocation traffic this is meant to remove.
        # numba compiles a strided variant instead.
        out = np.empty(hL.shape, dtype=np.float64)
        return _rusanov_jit(hL, huL, hvL, uL, vL, hR, huR, hvR, uR, vR,
                            normal_axis, out, np.empty_like(out),
                            np.empty_like(out), np.empty_like(out))

    cL = np.sqrt(_G * np.maximum(hL, 0.0))
    cR = np.sqrt(_G * np.maximum(hR, 0.0))

    if normal_axis == 0:
        a = np.maximum(np.abs(uL) + cL, np.abs(uR) + cR)
        f_h_L,  f_h_R  = huL, huR
        f_hu_L = huL * uL + 0.5 * _G * hL * hL
        f_hu_R = huR * uR + 0.5 * _G * hR * hR
        f_hv_L, f_hv_R = hvL * uL, hvR * uR
    else:
        a = np.maximum(np.abs(vL) + cL, np.abs(vR) + cR)
        f_h_L,  f_h_R  = hvL, hvR
        f_hu_L, f_hu_R = huL * vL, huR * vR
        f_hv_L = hvL * vL + 0.5 * _G * hL * hL
        f_hv_R = hvR * vR + 0.5 * _G * hR * hR

    f_h  = 0.5 * (f_h_L  + f_h_R)  - 0.5 * a * (hR  - hL)
    f_hu = 0.5 * (f_hu_L + f_hu_R) - 0.5 * a * (huR - huL)
    f_hv = 0.5 * (f_hv_L + f_hv_R) - 0.5 * a * (hvR - hvL)
    return f_h, f_hu, f_hv, a


def _ax(axis: int, start, stop) -> tuple:
    """Slice tuple selecting ``[start:stop]`` along ``axis``, all of the other."""
    s = [slice(None), slice(None)]
    s[axis] = slice(start, stop)
    return tuple(s)


def _rhs(h, hu, hv, z, dx, dy):
    """
    Spatial operator: returns ``(dh, dhu, dhv, max_wave_speed, edge_outflux)``.

    Well-balanced construction
    --------------------------
    Hydrostatic reconstruction, Audusse et al. (2004). The bed at an interface
    is single-valued — both sides see the *same* bed — and is taken as the
    **maximum** of the two adjoining cell values. Each cell's free surface is
    reconstructed against **its own** bed ``z``, and the bed-slope source is the
    pair of face corrections that construction requires, not a centred
    ``h_bar * dz``. Lake at rest is then exact over **arbitrary** topography.

    This replaced a mean-bed variant that took the interface bed as the average
    of the two cells and re-derived each cell's bed as the mean of its two
    interface values. That second step is ``z + lap(z)/4`` exactly, so the
    scheme reconstructed ``eta_true + lap(z)/4`` and preserved a lake at rest
    only where the bed's discrete Laplacian vanished — a flat or exactly linear
    bed. Everywhere else a spurious gradient drove flow: measured 70–96 m of
    free-surface drift and ~40 m/s in standing water on a walled pool, and a
    one-cell barrier passed 55 % of its pool in 300 s because no face in the
    domain sat at its crest. See the forensic audit, RC-1 through RC-4, and
    ``tests/test_well_balanced.py``, which pins the property on this function.

    Boundaries are zero-gradient (transmissive) via replicated ghost cells.
    This must not use ``np.roll``: roll wraps, silently making the domain
    periodic, so water leaving the east edge re-enters at the west across cells
    whose beds differ by kilometres.
    """
    ny, nx = h.shape
    pad = 2

    hp  = np.pad(h,  pad, mode="edge")
    hup = np.pad(hu, pad, mode="edge")
    hvp = np.pad(hv, pad, mode="edge")
    zp  = np.pad(z,  pad, mode="edge")

    up = _desingularise(hp, hup)
    vp = _desingularise(hp, hvp)

    dh  = np.zeros_like(h)
    dhu = np.zeros_like(h)
    dhv = np.zeros_like(h)
    outflux = 0.0
    gross_out = 0.0
    max_a = 0.0

    for axis, d in ((1, dx), (0, dy)):
        n = h.shape[1 - axis] if axis == 0 else h.shape[1]
        n = nx if axis == 1 else ny
        L = n + 2 * pad

        # Interface bed: single-valued, and the MAX of the two adjoining cells
        # -- Audusse et al. (2004) as published. The MEAN that stood here
        # averaged a barrier's crest away at its own upstream face: a 100 m
        # reservoir cell beside a 160 m wall gave a face bed of 130 m, so a pool
        # 5 m BELOW the crest saw 40 m of fictitious depth and a 19.81 m/s
        # gravity wave, and a one-cell wall had no face anywhere at its own
        # crest -- it did not exist to the flux operator (audit RC-2, RC-3).
        zi = np.maximum(zp[_ax(axis, None, -1)], zp[_ax(axis, 1, None)])   # L-1

        # Core = one ghost ring around the interior: padded indices 1..L-2.
        core = slice(1, L - 1)
        z_m = zi[_ax(axis, 0, n + 2)]        # bed at each core cell's low edge
        z_p = zi[_ax(axis, 1, n + 3)]        # ... and its high edge

        # The cell's OWN bed. This used to be `0.5 * (z_m + z_p)`, the mean of
        # the cell's two interface values, which expands to `z + lap(z)/4`
        # exactly -- so the free surface the scheme reconstructed was
        # `eta_true + lap(z)/4`, and the lake-at-rest state was preserved only
        # where the bed's discrete Laplacian vanished. On real conditioned
        # terrain that error averaged 2.1 m per axis at 28 m cells and 10.4 m at
        # the production 112 m (audit RC-1, RC-4).
        z_c = zp[_ax(axis, core.start, core.stop)]

        h_c  = hp[_ax(axis, core.start, core.stop)]
        u_c  = up[_ax(axis, core.start, core.stop)]
        v_c  = vp[_ax(axis, core.start, core.stop)]
        eta_c = h_c + z_c

        eta_m, eta_p = _edge_values(eta_c, axis)
        u_m,   u_p   = _edge_values(u_c,   axis)
        v_m,   v_p   = _edge_values(v_c,   axis)

        # Positivity: the reconstructed edge depth is `max(0, eta - z_face)`,
        # and nothing else. This is Audusse's `h*`.
        #
        # A cell-mean-preserving tilt used to stand here -- where an edge fell
        # below its face bed, the surface was rotated about the cell centre so
        # the dry edge sat on the bed and `h = (h_m + h_p)/2` was preserved. It
        # existed because the OLD source term was `g * h_bar * dz`, and that
        # form's cancellation rested on exactly that identity.
        #
        # The Audusse source above does not. It rests on the two face pressure
        # fluxes cancelling, which needs `h*` and nothing more. Worse, with the
        # interface bed now taken as the MAX, `eta_p < z_p` fires for every wet
        # cell that merely neighbours a barrier -- a full 55 m reservoir cell
        # beside a 160 m wall is not partially dry, but the tilt treated it as
        # such and rewrote its 55 m of water as 50 m, destroying the lake at
        # rest it was there to protect. Measured: it was the sole remaining
        # failure of `test_well_balanced.py` after the max-bed and own-bed
        # changes landed.
        h_m = np.maximum(eta_m - z_m, 0.0)
        h_p = np.maximum(eta_p - z_p, 0.0)

        # The same two edge values measured against the CELL's own bed rather
        # than the face bed. The bed-slope source below is the difference
        # between these two readings of the same water column, so it vanishes
        # identically wherever the cell bed and the face bed agree -- which is
        # everywhere on a flat bed, and everywhere the neighbour is not higher.
        hc_m = np.maximum(eta_m - z_c, 0.0)
        hc_p = np.maximum(eta_p - z_c, 0.0)

        # Interface k joins core cells k and k+1 and shares one bed value.
        hL, hR = h_p[_ax(axis, None, -1)], h_m[_ax(axis, 1, None)]
        uL, uR = u_p[_ax(axis, None, -1)], u_m[_ax(axis, 1, None)]
        vL, vR = v_p[_ax(axis, None, -1)], v_m[_ax(axis, 1, None)]

        f_h, f_hu, f_hv, a = _rusanov(
            hL, hL * uL, hL * vL, uL, vL,
            hR, hR * uR, hR * vR, uR, vR,
            normal_axis=(0 if axis == 1 else 1),
        )
        max_a = max(max_a, float(a.max()))

        # Interior cell i is core index i+1: left interface i, right interface i+1.
        F_h_l,  F_h_r  = f_h[_ax(axis, 0, n)],  f_h[_ax(axis, 1, n + 1)]
        F_hu_l, F_hu_r = f_hu[_ax(axis, 0, n)], f_hu[_ax(axis, 1, n + 1)]
        F_hv_l, F_hv_r = f_hv[_ax(axis, 0, n)], f_hv[_ax(axis, 1, n + 1)]

        # Trim the cross-axis ghost ring so shapes match the interior grid.
        # The ring is `pad` cells wide on each side, not one.
        cross = 1 - axis
        trim = _ax(cross, pad, -pad)
        F_h_l, F_h_r   = F_h_l[trim],  F_h_r[trim]
        F_hu_l, F_hu_r = F_hu_l[trim], F_hu_r[trim]
        F_hv_l, F_hv_r = F_hv_l[trim], F_hv_r[trim]

        cell = _ax(axis, 1, n + 1)

        # Bed-slope source, Audusse et al. (2004) eq. 2.16-2.17. With a
        # single-valued interface bed taken as the max, the source is NOT a
        # centred `h_bar * dz` term but a pair of face corrections,
        # `(g/2)(h_c^2 - h*^2)` at each of the cell's two faces. The `g/2 h_c^2`
        # halves cancel between them, leaving the difference of the
        # reconstructed edge depths squared.
        #
        # Each face contributes `(g/2)(h*^2 - h_edge^2)`: the pressure the face
        # actually carries, minus the pressure the cell's own column would
        # carry at that edge. Two properties follow, and BOTH are required:
        #
        #   flat bed  -> z_c == z_m == z_p, so h_edge == h* at both faces and
        #                the source is IDENTICALLY ZERO. It must be: a flat bed
        #                has no bed slope to source. Getting this wrong is not
        #                subtle -- it put Ritter RMSE at 1.379 m against a
        #                0.043 m bar, because a dam break has a large `dh` and
        #                the naive `(g/2)(h_p^2 - h_m^2)` form reads MUSCL's
        #                depth gradient as if it were a bed gradient.
        #
        #   at rest   -> eta is constant so `h_edge` is the same at both faces
        #                and cancels, leaving `(g/2)(h_p^2 - h_m^2)`, which is
        #                exactly the difference of the two pressure fluxes
        #                `_rusanov` returns. They annihilate, for ANY bed.
        # ...and it acts only where there is a water column for the bed to push
        # on. A dry cell has no column, so no bed reaction: without this mask
        # the reconstruction's leftovers on dry cells produced up to 200 m^2/s^2
        # of spurious `dhu`/`dhv` on real terrain, while `dh` stayed exactly
        # zero. That momentum cannot move water immediately -- `_desingularise`
        # reads zero velocity at zero depth -- but it sits on the cell waiting
        # to be realised as a velocity the moment the flood arrives and wets it.
        # Measured over WET cells the scheme was already exact to 4.5e-12; this
        # makes it exact over the dry ones too.
        wet_c = (h_c > _DRY)[cell][trim]
        src = np.where(
            wet_c,
            0.5 * _G * ((h_p * h_p - hc_p * hc_p)
                        - (h_m * h_m - hc_m * hc_m))[cell][trim],
            0.0,
        )

        dh  -= (F_h_r  - F_h_l)  / d
        dhu -= (F_hu_r - F_hu_l) / d
        dhv -= (F_hv_r - F_hv_l) / d
        if axis == 1:
            dhu += src / d
        else:
            dhv += src / d

        # Volume crossing the two outer faces, for the mass ledger.
        #
        # `outflux` is the NET flux and is what conservation needs: a
        # transmissive boundary can let water back in, and the residual is only
        # closed if that is subtracted. `gross_out` counts only the half that
        # actually leaves, which is the quantity that answers "can water leave
        # this domain at all" — the net alone cannot distinguish a sealed
        # domain from one where inflow and outflow happen to cancel.
        if axis == 1:
            hi, lo = F_h_r[:, -1], F_h_l[:, 0]
            outflux += float(hi.sum() - lo.sum()) * dy
            gross_out += (float(np.maximum(hi, 0.0).sum())
                          + float(np.maximum(-lo, 0.0).sum())) * dy
        else:
            hi, lo = F_h_r[-1, :], F_h_l[0, :]
            outflux += float(hi.sum() - lo.sum()) * dx
            gross_out += (float(np.maximum(hi, 0.0).sum())
                          + float(np.maximum(-lo, 0.0).sum())) * dx

    return dh, dhu, dhv, max_a, outflux, gross_out


def run_2d_swe_simulation(
    elevation_grid:    np.ndarray,
    dx_m:              float,
    dy_m:              float,
    inflow_x_idx:      int,
    inflow_y_idx:      int,
    hydrograph_t_s:    np.ndarray,
    hydrograph_Q_m3s:  np.ndarray,
    total_duration_s:  float = 7200.0,
    save_interval_s:   float = 300.0,
    manning_n:         Union[float, np.ndarray] = 0.045,
    scenario_name:     str = "simulation",
    initial_depth:     np.ndarray | None = None,
    cfl:               float = 0.35,
    arrival_depth_m:   float = 0.10,
    progress_cb=None,
    backend:           str = "cpu",
    hydrograph_v_ms:   np.ndarray | None = None,
    inflow_direction:  tuple[float, float] | None = None,
    inflows:           list[InflowBoundary] | None = None,
    breach_opening:    "BreachOpening | None" = None,
    upstream_cv_mask:  np.ndarray | None = None,
    stop_when_quiescent: bool = False,
    quiescent_speed_ms: float = 0.05,
    quiescent_hold_s:    float = 600.0,
) -> SimulationResult:
    """
    Advance the 2D shallow water equations over a DEM.

    Parameters
    ----------
    elevation_grid : bed elevation [m]. Must already be conditioned — no-data
        cells become sinks the flood drains into.
    dx_m, dy_m : cell size in **metres**. A geographic grid is a unit error.
    manning_n : scalar or terrain-classified roughness grid [s/m^(1/3)].
    initial_depth : water depth at t=0 [m]; defaults to a dry bed. Needed for
        classical benchmarks where water is an initial condition rather than an
        injected hydrograph.
    cfl : Courant number. 0.35 suits SSP-RK2 with MUSCL.
    arrival_depth_m : depth at which a cell is recorded as "reached".
    backend : "cpu" (default, numpy/numba — the validated path) or "gpu"
        (cupy — see swe_2d_gpu.py). "gpu" is opt-in only: nothing selects it
        automatically. If cupy or the CUDA device is unavailable, this falls
        back to "cpu" and logs why rather than failing the run — a solver that
        cannot run is worse than one that ran on the slower backend.
    hydrograph_v_ms : jet velocity [m/s] at each ``hydrograph_t_s`` sample,
        interpolated the same way as ``hydrograph_Q_m3s``. Estimated by the
        caller from continuity through the breach opening (``Q / (width *
        head)``). ``None`` (default) injects mass at rest, exactly as before
        this parameter existed.
    inflow_direction : unit vector ``(dir_x, dir_y)`` in grid index space that
        the injected momentum points along (the breach-to-downstream axis). A
        single fixed direction for the whole run — the breach axis does not
        rotate during one flood event. ``None`` (default) disables momentum
        injection regardless of ``hydrograph_v_ms``.
    stop_when_quiescent : when True, ``total_duration_s`` becomes a safety CAP
        rather than the target, and the run ends once the flood has actually
        stopped moving. A fixed window is arbitrary -- it either truncates a
        flood that is still running or spends wall time integrating still
        water. Quiescence requires ALL of: no boundary is still supplying
        water, the breach is no longer discharging, and the fastest wet cell in
        the domain is below ``quiescent_speed_ms`` -- held continuously for
        ``quiescent_hold_s`` of simulated time so a momentary lull between
        surges cannot end the run. ``SimulationResult.stopped_early`` and
        ``.end_time_s`` record what happened.
    quiescent_speed_ms : the domain is "still" below this speed [m/s].
    quiescent_hold_s : how long stillness must persist before stopping [s].
    inflows : optional list of ``InflowBoundary`` for multiple simultaneous
        inflow points (compound events with more than one breach/structure).
        When given, ``inflow_x_idx``/``inflow_y_idx``/``hydrograph_t_s``/
        ``hydrograph_Q_m3s``/``hydrograph_v_ms``/``inflow_direction`` are
        ignored and each boundary is injected independently, accumulating
        into the same ``h``/``hu``/``hv`` state. ``None`` (default) is the
        single scalar-inflow path, byte-identical to before this parameter
        existed.
    """
    if backend == "gpu":
        from . import swe_2d_gpu
        ok, info = swe_2d_gpu.gpu_available()
        if ok:
            logger.info("SWE solver: GPU backend requested and available (%s)", info)
            try:
                return swe_2d_gpu.run_2d_swe_simulation_gpu(
                    elevation_grid=elevation_grid, dx_m=dx_m, dy_m=dy_m,
                    inflow_x_idx=inflow_x_idx, inflow_y_idx=inflow_y_idx,
                    hydrograph_t_s=hydrograph_t_s, hydrograph_Q_m3s=hydrograph_Q_m3s,
                    total_duration_s=total_duration_s, save_interval_s=save_interval_s,
                    manning_n=manning_n, scenario_name=scenario_name,
                    initial_depth=initial_depth, cfl=cfl,
                    arrival_depth_m=arrival_depth_m, progress_cb=progress_cb,
                )
            except Exception as exc:                          # noqa: BLE001
                # gpu_available() checks at the START of the run. A 4 GB card
                # shared with the desktop compositor (WDDM) can still run out of
                # VRAM mid-run if something else on the same GPU (e.g. this
                # project's own browser-rendered map) grows while the solver is
                # working. A backend choice must never be able to take a whole
                # run down -- fall back and finish on CPU instead.
                logger.warning("SWE solver: GPU run failed mid-simulation (%s) "
                               "— retrying this arm on CPU", exc)
        else:
            logger.warning("SWE solver: GPU backend requested but unavailable (%s) "
                           "— falling back to CPU", info)

    ny, nx = elevation_grid.shape
    logger.info("SWE solver (well-balanced, MUSCL + SSP-RK2): %dx%d grid, "
                "dx=%.1f m dy=%.1f m, T=%.0f s", nx, ny, dx_m, dy_m, total_duration_s)

    # A copy, not a view: the breach opening erodes `z` in place during the
    # integration, and `np.ascontiguousarray` hands back the SAME array when
    # the input is already contiguous float64 -- which would silently rewrite
    # the caller's DEM.
    z = np.array(elevation_grid, dtype=np.float64, order="C", copy=True)

    n_grid = (np.full((ny, nx), float(manning_n), dtype=np.float64)
              if np.isscalar(manning_n)
              else np.asarray(manning_n, dtype=np.float64))

    if initial_depth is None:
        h = np.zeros((ny, nx), dtype=np.float64)
    else:
        h = np.maximum(np.asarray(initial_depth, dtype=np.float64), 0.0).copy()
        if h.shape != (ny, nx):
            raise ValueError(
                f"initial_depth shape {h.shape} does not match DEM {(ny, nx)}")

    hu = np.zeros((ny, nx), dtype=np.float64)
    hv = np.zeros((ny, nx), dtype=np.float64)

    cell_area = dx_m * dy_m
    vol_initial = float(h.sum() * cell_area)

    max_h = h.copy()
    # P4 (audit SS39): a cell that is under the reservoir at t = 0 has not been
    # "reached by the flood" at t = 0 -- it was already wet. Seeding
    # `arrival_time` with 0.0 there put the whole impoundment into the arrival
    # raster as instantly inundated, and two villages reported a max depth of
    # 58.0 m, which is exactly the reservoir depth. They keep NaN until the
    # flood actually adds depth over them.
    arrival_time = np.full(h.shape, np.nan)
    #: The t = 0 state, carried through so the consequence stage can subtract
    #: it. `max_depth_grid` deliberately still INCLUDES the reservoir -- the map
    #: should show the pool -- so the subtraction belongs downstream, not here.
    initial_depth_grid = h.copy()

    def _gaussian_mask(iy_c: int, ix_c: int) -> np.ndarray:
        """Gaussian inflow kernel over interior cells only, so injected water
        never lands on a boundary cell where it would immediately leave the
        domain. Same kernel as before Part 3; now reusable per boundary."""
        mask = np.zeros((ny, nx), dtype=np.float64)
        for di in range(-2, 3):
            for dj in range(-2, 3):
                iy = int(np.clip(iy_c + di, 1, ny - 2))
                ix = int(np.clip(ix_c + dj, 1, nx - 2))
                mask[iy, ix] += np.exp(-(di * di + dj * dj) / 2.0)
        mask /= mask.sum()
        return mask

    # Normalise to one internal list of boundaries, whether the caller used
    # the scalar single-inflow parameters (the original interface) or the
    # `inflows` list (Part 3). This keeps the injection code below a single
    # loop instead of two near-duplicate code paths.
    if inflows is not None:
        _boundaries = [
            (b.x_idx, b.y_idx, _gaussian_mask(b.y_idx, b.x_idx),
             b.hydrograph_t_s, b.hydrograph_Q_m3s, b.hydrograph_v_ms, b.direction)
            for b in inflows
        ]
    else:
        _boundaries = [
            (inflow_x_idx, inflow_y_idx, _gaussian_mask(inflow_y_idx, inflow_x_idx),
             hydrograph_t_s, hydrograph_Q_m3s, hydrograph_v_ms, inflow_direction)
        ]
    # ── P3: an opening REPLACES the injection, it never accompanies it ────────
    # Keeping both would reinstate defect C1 -- the impoundment supplied once as
    # `initial_depth` and again as an injected hydrograph, measured at exactly
    # 2.00x. The audit (SS41) bans running them side by side "for comparison",
    # so this refuses loudly rather than silently double counting.
    if breach_opening is not None:
        live = [b for b in _boundaries
                if float(np.max(np.asarray(b[4], dtype=float), initial=0.0)) > 0.0]
        if live:
            raise ValueError(
                "breach_opening and a non-zero inflow hydrograph were both supplied. "
                "The opening drains the impoundment that is already in the domain; "
                "injecting the same water again is defect C1 (measured 2.00x). "
                "Pass one or the other."
            )
        _boundaries = []

    # `z` is no longer read-only: the opening erodes it. Keep the pre-emplacement
    # bed as the floor erosion cannot cut below.
    _opening_floor = None
    if breach_opening is not None:
        _opening_floor = np.maximum(
            np.asarray(breach_opening.z_natural, dtype=np.float64),
            float(breach_opening.invert_final_m))
        if breach_opening.axis_distance_m is not None:
            _axis_d = np.asarray(breach_opening.axis_distance_m, dtype=np.float64)
        else:
            rr, cc = np.nonzero(breach_opening.mask)
            r0, c0 = (float(rr.mean()), float(cc.mean())) if rr.size else (0.0, 0.0)
            ii, jj = np.indices(z.shape)
            _axis_d = np.hypot((ii - r0) * dy_m, (jj - c0) * dx_m)

    # Seed point(s) the active window must always contain, so the injection
    # kernel of every boundary is never clipped.
    _seed_points = [(iy, ix) for ix, iy, *_ in _boundaries]
    if breach_opening is not None:
        rr, cc = np.nonzero(breach_opening.mask)
        if rr.size:
            _seed_points += [(int(rr.min()), int(cc.min())),
                             (int(rr.max()), int(cc.max()))]
    if not _seed_points:
        _seed_points = [(ny // 2, nx // 2)]

    saved_times: list[float] = []
    saved_h: list[np.ndarray] = []
    saved_u: list[np.ndarray] = []
    saved_v: list[np.ndarray] = []

    t = 0.0
    next_save = 0.0
    # progress_cb(t_simulated_s, total_s) -- reported on a wall-clock throttle so
    # the caller gets a steady signal regardless of how coarse save_interval_s is.
    _last_cb = time.time()
    step = 0
    vol_injected = 0.0
    vol_outflow = 0.0
    vol_outflow_gross = 0.0
    vol_clipped = 0.0
    t_wall = time.time()

    def _window_for_all_seeds(h_, prev=None):
        """`_active_window`, unioned across every inflow seed point.

        `_active_window` itself is untouched (protected component) — this
        only combines its single-seed result once per seed point, so every
        boundary's injection kernel stays inside the window with a single
        scalar inflow this reduces to exactly one `_active_window` call.
        """
        wins = [_active_window(h_, iy, ix, prev=prev) for iy, ix in _seed_points]
        sy0 = min(w[0].start for w in wins); sy1 = max(w[0].stop for w in wins)
        sx0 = min(w[1].start for w in wins); sx1 = max(w[1].stop for w in wins)
        return slice(sy0, sy1), slice(sx0, sx1)

    vol_bed_lowering = 0.0
    cv = None if upstream_cv_mask is None else np.asarray(upstream_cv_mask, dtype=bool)
    q_t: list[float] = []
    q_meas: list[float] = []
    q_invert: list[float] = []
    q_width: list[float] = []
    _cv_prev = float(h[cv].sum() * cell_area) if cv is not None else 0.0
    _cv_lowering_acc = 0.0

    win = _window_for_all_seeds(h)
    # Quiescence tracking. `_quiet_s` accumulates only while every stillness
    # condition holds; any motion resets it to zero, so a lull between surges
    # cannot end the run.
    _quiet_s = 0.0
    _stopped_early = False

    while t < total_duration_s:
        # ── Active window ────────────────────────────────────────────────────
        # Rebuilt on a step count, and immediately if water has reached the
        # perimeter. Everything below operates on this box; the rest of the
        # domain is dry and its update is identically zero.
        if step % _WINDOW_EVERY == 0 or _window_breached(h, win):
            win = _window_for_all_seeds(h, prev=win)
        sy, sx = win
        hw, huw, hvw = h[sy, sx], hu[sy, sx], hv[sy, sx]
        zw = z[sy, sx]
        nw = n_grid[sy, sx]

        # ── Time step from the CFL condition ─────────────────────────────────
        # Over the window only: _desingularise already returns zero below _DRY,
        # so the cells left out contribute 0 + sqrt(g*0) = 0 to this maximum.
        c = np.sqrt(_G * np.maximum(hw, 0.0))
        u_now = _desingularise(hw, huw)
        v_now = _desingularise(hw, hvw)
        max_wave = float(np.max(np.sqrt(u_now**2 + v_now**2) + c))
        max_wave = max(max_wave, 0.5)
        dt = min(cfl * min(dx_m, dy_m) / max_wave, 5.0, total_duration_s - t)
        dt = max(dt, 1e-3)

        # ── Inflow: mass, and — when a jet velocity/direction is supplied —
        # the momentum that mass actually arrives with. Looped over every
        # boundary (one iteration for the original scalar-inflow interface,
        # more for Part 3's `inflows` list), accumulating into the same
        # shared hw/huw/hvw each step.
        for _ix, _iy, _mask, _t_s, _Q_m3s, _v_ms, _dir in _boundaries:
            Q_now = float(np.interp(t, _t_s, _Q_m3s))
            if Q_now <= 0.0:
                continue
            # The window always contains every inflow point and the halo
            # exceeds the kernel radius, so no injected mass falls outside it.
            add_h = (Q_now / cell_area) * dt * _mask[sy, sx]
            hw = hw + add_h
            if _dir is not None and _v_ms is not None:
                # Momentum arrives with the mass: v_jet_now is a single scalar
                # per timestep (continuity through the breach opening,
                # Q/(width*head), computed once by the caller), applied
                # uniformly across the injection footprint. hu = h*u has units
                # m^2/s, so d(hu) = add_h [m] * v_jet_now [m/s] * dir_x.
                v_jet_now = float(np.interp(t, _t_s, _v_ms))
                if v_jet_now > 0.0:
                    huw = huw + add_h * v_jet_now * _dir[0]
                    hvw = hvw + add_h * v_jet_now * _dir[1]
            vol_injected += Q_now * dt

        # ── The opening erodes: `z` changes under the water ──────────────────
        # Two things need care here (audit SS38.3).
        #
        # 1. Lowering the bed under standing water must hold the FREE SURFACE,
        #    not the depth. eta = z + h; if z drops by d and h is held, eta
        #    drops with it and the pool has silently lost d metres of head. If
        #    h grows by d instead, eta is unchanged and the same water now sits
        #    in a deeper container. That is a storage re-attribution, not an
        #    inflow, so it is booked to its own counter and closes the ledger
        #    explicitly rather than disappearing into `clipped`.
        #
        # 2. Well-balancedness survives this. `_rhs` re-derives the interface
        #    bed from `z` on every call, so a `z` that changes between calls is
        #    consistent by construction -- but the lake-at-rest benchmark is
        #    re-run after this change, because that exact cancellation is the
        #    property that makes this solver worth keeping.
        if breach_opening is not None:
            _inv, _wid = breach_opening.state_at(t)
            if _wid > 0.0:
                opening_cells = breach_opening.mask & (_axis_d <= 0.5 * _wid)
                if opening_cells.any():
                    z_target = np.maximum(_opening_floor, _inv)
                    drop = np.where(opening_cells, z[...] - z_target, 0.0)
                    np.maximum(drop, 0.0, out=drop)
                    if drop.any():
                        wet_now = h > _DRY
                        # hold eta on wet cells; a dry cell just gets a lower bed
                        h += np.where(wet_now, drop, 0.0)
                        added = float((drop * wet_now).sum() * cell_area)
                        vol_bed_lowering += added
                        if cv is not None:
                            _cv_lowering_acc += float(
                                (drop * wet_now * cv).sum() * cell_area)
                        z -= drop
                        # the window's views are stale once z and h changed
                        hw, huw, hvw = h[sy, sx], hu[sy, sx], hv[sy, sx]
                        zw = z[sy, sx]

        # ── SSP-RK2 (Heun) ───────────────────────────────────────────────────
        dh1, dhu1, dhv1, _, out1, gout1 = _rhs(hw, huw, hvw, zw, dx_m, dy_m)
        h1  = hw  + dt * dh1
        hu1 = huw + dt * dhu1
        hv1 = hvw + dt * dhv1
        neg = np.minimum(h1, 0.0)
        vol_clipped += float(neg.sum() * cell_area)
        h1 = np.maximum(h1, 0.0)
        hu1 = np.where(h1 > _DRY, hu1, 0.0)
        hv1 = np.where(h1 > _DRY, hv1, 0.0)

        dh2, dhu2, dhv2, _, out2, gout2 = _rhs(h1, hu1, hv1, zw, dx_m, dy_m)
        h_new  = 0.5 * (hw  + h1  + dt * dh2)
        hu_new = 0.5 * (huw + hu1 + dt * dhu2)
        hv_new = 0.5 * (hvw + hv1 + dt * dhv2)

        neg = np.minimum(h_new, 0.0)
        vol_clipped += float(neg.sum() * cell_area)
        h_new = np.maximum(h_new, 0.0)
        vol_outflow += 0.5 * (out1 + out2) * dt
        vol_outflow_gross += 0.5 * (gout1 + gout2) * dt

        # ── Semi-implicit friction ───────────────────────────────────────────
        # Explicit Manning friction is stiff in thin films: the h^(4/3) in the
        # denominator makes the term explode as depth goes to zero, which is
        # what the old hard 20 m/s velocity clamp existed to suppress. Solving
        # it point-implicitly is unconditionally stable, so the clamp is gone.
        wet = h_new > _EPS
        u_new = _desingularise(h_new, hu_new)
        v_new = _desingularise(h_new, hv_new)
        speed = np.sqrt(u_new**2 + v_new**2)
        denom = np.ones_like(h_new)
        h_safe = np.maximum(h_new, _EPS)
        denom[wet] = 1.0 + dt * _G * nw[wet]**2 * speed[wet] / h_safe[wet]**(4.0 / 3.0)
        u_new = u_new / denom
        v_new = v_new / denom

        hu_new = h_new * u_new
        hv_new = h_new * v_new
        hu_new = np.where(h_new > _DRY, hu_new, 0.0)
        hv_new = np.where(h_new > _DRY, hv_new, 0.0)

        # Write the window back. Cells outside it were dry and stay dry, so
        # the untouched remainder of the domain is already correct.
        h[sy, sx] = h_new
        hu[sy, sx] = hu_new
        hv[sy, sx] = hv_new

        # A diverged solve must SAY it diverged. Without this the run completes
        # normally and returns NaN depths, a NaN `volume_clipped_m3` and a NaN
        # mass-closure error; the validity gates then fail closed only by
        # accident (`nan <= tol` is False) and report "G2 manufactured mass:
        # clipping created nan m^3", which names the wrong defect. Measured at
        # cfl=2.5 on a 60 m bed step (audit Part VI N-8). Production runs at
        # cfl=0.35, three orders from this, so the check is effectively free.
        if not np.isfinite(h_new).all():
            raise FloatingPointError(
                f"the solve diverged: non-finite depth at step {step}, "
                f"t = {t:.3f} s, dt = {dt:.4f} s. This is a CFL/stability "
                f"failure, not a clipping-tolerance breach — do not raise the "
                f"clipping tolerance to get past it.")

        np.maximum(max_h[sy, sx], h_new, out=max_h[sy, sx])
        arr_w = arrival_time[sy, sx]
        arr_w[(h_new >= arrival_depth_m) & np.isnan(arr_w)] = t
        # ── Q becomes a MEASUREMENT (audit SS38.4) ───────────────────────────
        # The rate at which the upstream control volume loses water. For a
        # conservative scheme this equals the integral of the face fluxes across
        # that volume's boundary (divergence theorem), and once the barrier
        # holds (validity gate G4) the only place that boundary is open is the
        # opening itself -- so this IS the breach discharge, not a proxy for it.
        # The bed-lowering re-attribution inside the volume is subtracted, or
        # eroding the breach would register as an inflow.
        #
        # ponytail: control-volume form rather than summing `F_h` over the
        # opening's downstream faces. It needs no `_rhs` signature change and no
        # window-to-global index mapping, and is exact for this scheme. Swap to
        # explicit face fluxes if the opening ever needs a PER-FACE breakdown.
        if cv is not None:
            _cv_now = float(h[cv].sum() * cell_area)
            _q = (_cv_prev + _cv_lowering_acc - _cv_now) / dt if dt > 0 else 0.0
            _cv_prev, _cv_lowering_acc = _cv_now, 0.0
            # The first step runs on a sub-second dt while the initial condition
            # settles, so dividing its storage change by that dt reports a
            # meaningless spike (measured 9.1e5 m3/s against a 9.2e2 m3/s mean).
            # It is a startup artefact of the differencing, not discharge.
            if step == 0:
                _q = 0.0
            _inv_now, _wid_now = (breach_opening.state_at(t)
                                  if breach_opening is not None else (float("nan"), 0.0))
            q_t.append(float(t)); q_meas.append(float(_q))
            q_invert.append(float(_inv_now)); q_width.append(float(_wid_now))

        t += dt
        step += 1

        if progress_cb is not None:
            _now = time.time()
            if _now - _last_cb >= 1.0:
                _last_cb = _now
                try:
                    progress_cb(float(t), float(total_duration_s))
                except Exception:      # noqa: BLE001
                    # Reporting progress must never be able to kill a run that
                    # is otherwise fine.
                    pass

        if t >= next_save or t >= total_duration_s:
            saved_times.append(float(t))
            saved_h.append(h.copy())
            saved_u.append(_desingularise(h, hu))
            saved_v.append(_desingularise(h, hv))
            next_save += save_interval_s
            logger.info("  t=%6.0f s (%5.1f min)  max_h=%5.2f m  wet=%d  dt=%.2f s",
                        t, t / 60.0, float(h.max()), int((h > arrival_depth_m).sum()), dt)

        # ── Has the flood stopped? ───────────────────────────────────────────
        # Three conditions, all required. Supply first: a domain can be very
        # still while a hydrograph is about to deliver its peak, so "nothing is
        # moving" alone is not enough.
        if stop_when_quiescent:
            # "Still supplying" means water is yet to arrive, not merely that
            # some is arriving right now. A hydrograph sitting at zero before a
            # later pulse would otherwise let the run stop before the flood
            # even started -- pinned by test_quiescence_stop.py.
            still_supplying = False
            for _ix, _iy, _mask, _t_s, _Q_m3s, _v_ms, _dir in _boundaries:
                _future = np.asarray(_Q_m3s, dtype=float)[np.asarray(_t_s, dtype=float) >= t]
                if float(np.interp(t, _t_s, _Q_m3s)) > 0.0 or (
                        _future.size and float(_future.max()) > 0.0):
                    still_supplying = True
                    break
            if breach_opening is not None and not still_supplying:
                # The opening is still a source while it is widening or while
                # measured discharge has not died away.
                if t < float(breach_opening.trigger_s) + float(breach_opening.formation_s):
                    still_supplying = True
                elif q_meas and abs(float(q_meas[-1])) > 1.0:
                    still_supplying = True

            wet_now = h > arrival_depth_m
            if wet_now.any():
                spd = np.hypot(_desingularise(h, hu), _desingularise(h, hv))
                max_speed = float(spd[wet_now].max())
            else:
                max_speed = 0.0

            if still_supplying or max_speed > quiescent_speed_ms:
                _quiet_s = 0.0
            else:
                _quiet_s += dt
                if _quiet_s >= quiescent_hold_s:
                    _stopped_early = True
                    logger.info(
                        "  quiescent: no supply and max speed %.4f m/s < %.3f "
                        "for %.0f s -> stopping at t=%.0f s (%.1f min); cap was "
                        "%.0f s", max_speed, quiescent_speed_ms, _quiet_s, t,
                        t / 60.0, total_duration_s)
                    if not saved_times or saved_times[-1] < t:
                        saved_times.append(float(t)); saved_h.append(h.copy())
                        saved_u.append(_desingularise(h, hu))
                        saved_v.append(_desingularise(h, hv))
                    break

    res = SimulationResult(
        times_s=saved_times, depth_grids=saved_h, u_grids=saved_u, v_grids=saved_v,
        max_depth_grid=max_h, arrival_time_s_grid=arrival_time,
        dx_m=dx_m, dy_m=dy_m, elevation_grid=z, scenario_name=scenario_name,
        initial_depth_grid=initial_depth_grid,
        volume_injected_m3=vol_injected, volume_outflow_m3=vol_outflow,
        volume_clipped_m3=abs(vol_clipped), volume_initial_m3=vol_initial,
        volume_outflow_gross_m3=vol_outflow_gross,
        volume_bed_lowering_m3=vol_bed_lowering,
        end_time_s=float(t), stopped_early=bool(_stopped_early),
        breach_q_t_s=q_t, breach_q_m3s=q_meas,
        breach_invert_m=q_invert, breach_width_m=q_width,
    )
    mc = res.mass_closure()
    logger.info("Simulation complete: %d steps in %.2f s  max_depth=%.2f m",
                step, time.time() - t_wall, float(max_h.max()))
    logger.info("  mass: in %.3e m^3, stored %.3e, outflow %.3e, clip %.2e "
                "-> closure %.3f%%",
                mc["initial_m3"] + mc["injected_m3"], mc["stored_m3"],
                mc["outflow_m3"], mc["clipped_m3"], mc["relative_error"] * 100.0)
    return res
