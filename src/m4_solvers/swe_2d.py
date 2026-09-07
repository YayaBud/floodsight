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
    #: Volume bookkeeping, filled in by the integrator.
    volume_injected_m3:  float = 0.0
    volume_outflow_m3:   float = 0.0
    volume_clipped_m3:   float = 0.0
    volume_initial_m3:   float = 0.0

    def mass_closure(self) -> dict:
        """Volume in vs volume accounted for, as a relative error."""
        total_in = self.volume_initial_m3 + self.volume_injected_m3
        stored = float(self.depth_grids[-1].sum() * self.dx_m * self.dy_m) \
            if self.depth_grids else 0.0
        residual = total_in - stored - self.volume_outflow_m3 + self.volume_clipped_m3
        return {
            "initial_m3":   self.volume_initial_m3,
            "injected_m3":  self.volume_injected_m3,
            "stored_m3":    stored,
            "outflow_m3":   self.volume_outflow_m3,
            "clipped_m3":   self.volume_clipped_m3,
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


def _lo(axis: int) -> tuple:
    """Slice selecting all interfaces' left-hand cells along ``axis``."""
    s = [slice(None), slice(None)]
    s[axis] = slice(None, -1)
    return tuple(s)


def _hi(axis: int) -> tuple:
    """Slice selecting all interfaces' right-hand cells along ``axis``."""
    s = [slice(None), slice(None)]
    s[axis] = slice(1, None)
    return tuple(s)


def _interior(axis: int, offset: int, n_along: int) -> tuple:
    """
    Interior slice: ``n_along`` entries from ``offset`` along ``axis``, and the
    ghost ring stripped from the other axis.

    Both axes must be trimmed. Slicing only the working axis leaves the padded
    rows attached and the result no longer matches the interior grid.
    """
    s = [slice(1, -1), slice(1, -1)]
    s[axis] = slice(offset, offset + n_along)
    return tuple(s)


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
    The bed is treated as **continuous** piecewise-linear: the bed elevation at
    an interface is the average of the two adjoining cell values, so both sides
    of an interface see the *same* bed. Each cell's bed is then re-derived as
    the mean of its two interface values, and the free surface is reconstructed
    relative to that. With a single-valued interface bed, the pressure flux
    difference and the bed-slope source cancel algebraically for a flat free
    surface — no correction terms, and no ``max()`` that would break the
    cancellation at the shoreline.

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
    max_a = 0.0

    for axis, d in ((1, dx), (0, dy)):
        n = h.shape[1 - axis] if axis == 0 else h.shape[1]
        n = nx if axis == 1 else ny
        L = n + 2 * pad

        # Interface bed elevations: single-valued, hence continuous.
        zi = 0.5 * (zp[_ax(axis, None, -1)] + zp[_ax(axis, 1, None)])   # L-1

        # Core = one ghost ring around the interior: padded indices 1..L-2.
        core = slice(1, L - 1)
        z_m = zi[_ax(axis, 0, n + 2)]        # bed at each core cell's low edge
        z_p = zi[_ax(axis, 1, n + 3)]        # ... and its high edge
        z_bar = 0.5 * (z_m + z_p)

        h_c  = hp[_ax(axis, core.start, core.stop)]
        u_c  = up[_ax(axis, core.start, core.stop)]
        v_c  = vp[_ax(axis, core.start, core.stop)]
        eta_c = h_c + z_bar

        eta_m, eta_p = _edge_values(eta_c, axis)
        u_m,   u_p   = _edge_values(u_c,   axis)
        v_m,   v_p   = _edge_values(v_c,   axis)

        # Partially dry cells: tilt the surface back so the dry edge sits on the
        # bed while the CELL MEAN IS PRESERVED. Clamping the depth with max(.,0)
        # instead would destroy h = (h_m + h_p)/2, which is the identity the
        # well-balanced cancellation rests on — that was the source of the
        # residual shoreline currents.
        dry_p = eta_p < z_p
        eta_m = np.where(dry_p, 2.0 * eta_c - z_p, eta_m)
        eta_p = np.where(dry_p, z_p, eta_p)

        dry_m = eta_m < z_m
        eta_p = np.where(dry_m, 2.0 * eta_c - z_m, eta_p)
        eta_m = np.where(dry_m, z_m, eta_m)

        h_m = np.maximum(eta_m - z_m, 0.0)
        h_p = np.maximum(eta_p - z_p, 0.0)

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
        h_bar = (0.5 * (h_m + h_p))[cell][trim]
        dz    = (z_p - z_m)[cell][trim]

        dh  -= (F_h_r  - F_h_l)  / d
        dhu -= (F_hu_r - F_hu_l) / d
        dhv -= (F_hv_r - F_hv_l) / d
        if axis == 1:
            dhu -= _G * h_bar * dz / d
        else:
            dhv -= _G * h_bar * dz / d

        # Net volume leaving through the two outer faces, for the mass ledger.
        if axis == 1:
            outflux += float(F_h_r[:, -1].sum() - F_h_l[:, 0].sum()) * dy
        else:
            outflux += float(F_h_r[-1, :].sum() - F_h_l[0, :].sum()) * dx

    return dh, dhu, dhv, max_a, outflux


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

    z = np.ascontiguousarray(elevation_grid, dtype=np.float64)

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
    arrival_time = np.where(h >= arrival_depth_m, 0.0, np.nan)

    # Gaussian inflow kernel over interior cells only, so injected water never
    # lands on a boundary cell where it would immediately leave the domain.
    inflow_mask = np.zeros((ny, nx), dtype=np.float64)
    for di in range(-2, 3):
        for dj in range(-2, 3):
            iy = int(np.clip(inflow_y_idx + di, 1, ny - 2))
            ix = int(np.clip(inflow_x_idx + dj, 1, nx - 2))
            inflow_mask[iy, ix] += np.exp(-(di * di + dj * dj) / 2.0)
    inflow_mask /= inflow_mask.sum()

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
    vol_clipped = 0.0
    t_wall = time.time()

    win = _active_window(h, inflow_y_idx, inflow_x_idx)
    while t < total_duration_s:
        # ── Active window ────────────────────────────────────────────────────
        # Rebuilt on a step count, and immediately if water has reached the
        # perimeter. Everything below operates on this box; the rest of the
        # domain is dry and its update is identically zero.
        if step % _WINDOW_EVERY == 0 or _window_breached(h, win):
            win = _active_window(h, inflow_y_idx, inflow_x_idx, prev=win)
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

        # ── Inflow: mass, and the momentum that mass arrives with ────────────
        Q_now = float(np.interp(t, hydrograph_t_s, hydrograph_Q_m3s))
        if Q_now > 0.0:
            # The window always contains the inflow point and the halo exceeds
            # the kernel radius, so no injected mass falls outside it.
            add_h = (Q_now / cell_area) * dt * inflow_mask[sy, sx]
            hw = hw + add_h
            vol_injected += Q_now * dt

        # ── SSP-RK2 (Heun) ───────────────────────────────────────────────────
        dh1, dhu1, dhv1, _, out1 = _rhs(hw, huw, hvw, zw, dx_m, dy_m)
        h1  = hw  + dt * dh1
        hu1 = huw + dt * dhu1
        hv1 = hvw + dt * dhv1
        neg = np.minimum(h1, 0.0)
        vol_clipped += float(neg.sum() * cell_area)
        h1 = np.maximum(h1, 0.0)
        hu1 = np.where(h1 > _DRY, hu1, 0.0)
        hv1 = np.where(h1 > _DRY, hv1, 0.0)

        dh2, dhu2, dhv2, _, out2 = _rhs(h1, hu1, hv1, zw, dx_m, dy_m)
        h_new  = 0.5 * (hw  + h1  + dt * dh2)
        hu_new = 0.5 * (huw + hu1 + dt * dhu2)
        hv_new = 0.5 * (hvw + hv1 + dt * dhv2)

        neg = np.minimum(h_new, 0.0)
        vol_clipped += float(neg.sum() * cell_area)
        h_new = np.maximum(h_new, 0.0)
        vol_outflow += 0.5 * (out1 + out2) * dt

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

        np.maximum(max_h[sy, sx], h_new, out=max_h[sy, sx])
        arr_w = arrival_time[sy, sx]
        arr_w[(h_new >= arrival_depth_m) & np.isnan(arr_w)] = t
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

    res = SimulationResult(
        times_s=saved_times, depth_grids=saved_h, u_grids=saved_u, v_grids=saved_v,
        max_depth_grid=max_h, arrival_time_s_grid=arrival_time,
        dx_m=dx_m, dy_m=dy_m, elevation_grid=z, scenario_name=scenario_name,
        volume_injected_m3=vol_injected, volume_outflow_m3=vol_outflow,
        volume_clipped_m3=abs(vol_clipped), volume_initial_m3=vol_initial,
    )
    mc = res.mass_closure()
    logger.info("Simulation complete: %d steps in %.2f s  max_depth=%.2f m",
                step, time.time() - t_wall, float(max_h.max()))
    logger.info("  mass: in %.3e m^3, stored %.3e, outflow %.3e, clip %.2e "
                "-> closure %.3f%%",
                mc["initial_m3"] + mc["injected_m3"], mc["stored_m3"],
                mc["outflow_m3"], mc["clipped_m3"], mc["relative_error"] * 100.0)
    return res
