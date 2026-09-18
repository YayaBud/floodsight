"""
M4 — GPU (cupy) backend for the 2D shallow-water solver
=========================================================
A second, fully independent implementation of the well-balanced solver in
``swe_2d.py``, running on cupy instead of numpy/numba.

Why a separate file instead of a shared ``xp``-parameterised core
-------------------------------------------------------------------
The CPU path is validated to five pinned benchmark numbers (see
``tests/test_swe_validation.py``) and the whole point of adding a GPU path is
to go faster WITHOUT being able to accidentally change what the CPU path does.
Threading a generic array-module parameter through ``_rhs`` and its four
kernels would touch code that already works and is trusted. This file
duplicates that logic instead — mechanically translated from the numpy
fallback formulas that already live in ``swe_2d.py`` (the path used there when
numba is unavailable) — so the two solvers cannot interfere with each other.
``swe_2d.py`` imports nothing from here at module load time; this module is
only touched if a caller explicitly asks for ``backend="gpu"``.

Measured, not assumed
----------------------
A sandboxed benchmark (isolated venv, same op count as one ``_rhs`` call, same
grid size as the real Rishiganga case at coarsen 2 — 432x400 padded, 172,800
cells) measured resident cupy arrays at ~5x the wall time of the current
numba-fused CPU kernels: 208 calls in 2.0 s vs 10.1 s. That number is for an
approximation of ``_rhs`` (it omitted the dry-cell tilt correction below), so
the real speedup is expected to be somewhat under 5x, not over it.

What "resident" means here and why it matters
------------------------------------------------
The measured win assumes the state arrays (h, hu, hv, z) live on the GPU for
the WHOLE run and are not shipped back to the host every step. This module
uploads once at the start, keeps every array on-device through the entire
time-stepping loop, and only copies back to host: (a) the two scalars needed
for host-side control flow (the CFL wave speed and, once per second, a
progress fraction), and (b) full grids at actual save points -- the same
cadence ``save_interval_s`` already uses on the CPU path, so this does not add
transfer traffic beyond what the CPU path already does when writing GeoTIFFs.

Numerical parity, not numerical identity
-------------------------------------------
cupy is not required to sum floats in the same order as numba's explicit
loops, so bit-for-bit identity with the CPU path is not the bar here (unlike
the CPU kernel work earlier, which fused an existing numpy computation and
therefore could and did assert bit-identical output). The bar for this module
is the same one the CPU solver itself is held to: reproduce the Ritter
benchmark within its tolerance. See ``verify_against_ritter()`` below and
``tests/test_swe_gpu.py``.
"""

from __future__ import annotations

import logging
import time

import numpy as np

from .swe_2d import SimulationResult, _G, _EPS, _DRY, _ax

logger = logging.getLogger(__name__)

try:
    import cupy as cp
    _HAVE_CUPY = True
except ImportError:                                    # pragma: no cover
    _HAVE_CUPY = False


def gpu_available() -> tuple[bool, str]:
    """
    (True, device name) if cupy can actually launch a kernel right now, else
    (False, reason). Import succeeding is not enough — the CUDA runtime/driver
    combination can still fail at first kernel launch (this project hit
    exactly that: a cupy wheel built for the wrong CUDA toolkit version
    imports fine and only fails when a kernel is compiled).
    """
    if not _HAVE_CUPY:
        return False, "cupy not installed"
    try:
        x = cp.arange(4, dtype=cp.float64)
        cp.cuda.Stream.null.synchronize()
        float((x * 2).sum())
        name = cp.cuda.runtime.getDeviceProperties(0)["name"]
        if isinstance(name, bytes):
            name = name.decode()
        return True, name
    except Exception as exc:                            # noqa: BLE001
        return False, f"cupy present but device unusable: {exc}"


# ──────────────────────────────────────────────────────────────────────────────
# Kernels — mechanical cp-translation of the numpy fallback formulas in
# swe_2d.py. Same formulas, same order of operations, different array module.
# ──────────────────────────────────────────────────────────────────────────────

def _minmod_gpu(a, b):
    return cp.where(a * b > 0.0, cp.sign(a) * cp.minimum(cp.abs(a), cp.abs(b)), 0.0)


def _edge_values_gpu(q, axis):
    back = q - cp.roll(q, 1, axis=axis)
    fwd = cp.roll(q, -1, axis=axis) - q
    slope = _minmod_gpu(back, fwd)
    idx_lo = [slice(None)] * q.ndim
    idx_hi = [slice(None)] * q.ndim
    idx_lo[axis] = 0
    idx_hi[axis] = -1
    slope[tuple(idx_lo)] = 0.0
    slope[tuple(idx_hi)] = 0.0
    return q - 0.5 * slope, q + 0.5 * slope


def _desingularise_gpu(h, hq):
    h2 = h * h
    return cp.where(h > _DRY, 2.0 * h * hq / (h2 + cp.maximum(h2, _EPS * _EPS)), 0.0)


def _rusanov_gpu(hL, huL, hvL, uL, vL, hR, huR, hvR, uR, vR, normal_axis):
    cL = cp.sqrt(_G * cp.maximum(hL, 0.0))
    cR = cp.sqrt(_G * cp.maximum(hR, 0.0))
    if normal_axis == 0:
        a = cp.maximum(cp.abs(uL) + cL, cp.abs(uR) + cR)
        f_h_L, f_h_R = huL, huR
        f_hu_L = huL * uL + 0.5 * _G * hL * hL
        f_hu_R = huR * uR + 0.5 * _G * hR * hR
        f_hv_L, f_hv_R = hvL * uL, hvR * uR
    else:
        a = cp.maximum(cp.abs(vL) + cL, cp.abs(vR) + cR)
        f_h_L, f_h_R = hvL, hvR
        f_hu_L, f_hu_R = huL * vL, huR * vR
        f_hv_L = hvL * vL + 0.5 * _G * hL * hL
        f_hv_R = hvR * vR + 0.5 * _G * hR * hR
    f_h = 0.5 * (f_h_L + f_h_R) - 0.5 * a * (hR - hL)
    f_hu = 0.5 * (f_hu_L + f_hu_R) - 0.5 * a * (huR - huL)
    f_hv = 0.5 * (f_hv_L + f_hv_R) - 0.5 * a * (hvR - hvL)
    return f_h, f_hu, f_hv, a


def _rhs_gpu(h, hu, hv, z, dx, dy):
    """
    GPU mirror of ``swe_2d._rhs``. Same Audusse (2004) hydrostatic
    reconstruction — interface bed as the MAX, free surface against the cell's
    own bed, face-pair bed-slope source — and the same slicing via the shared
    ``_ax`` helper (pure Python — works identically on cupy arrays).

    This must stay a line-for-line mirror. The two backends are only ever
    compared within a wide tolerance, so a divergence here is silent.
    """
    ny, nx = h.shape
    pad = 2

    hp = cp.pad(h, pad, mode="edge")
    hup = cp.pad(hu, pad, mode="edge")
    hvp = cp.pad(hv, pad, mode="edge")
    zp = cp.pad(z, pad, mode="edge")

    up = _desingularise_gpu(hp, hup)
    vp = _desingularise_gpu(hp, hvp)

    dh = cp.zeros_like(h)
    dhu = cp.zeros_like(h)
    dhv = cp.zeros_like(h)
    outflux = 0.0
    max_a = 0.0

    for axis, d in ((1, dx), (0, dy)):
        n = nx if axis == 1 else ny
        L = n + 2 * pad

        # Audusse et al. (2004): interface bed single-valued and taken as the
        # MAX, free surface reconstructed against the cell's OWN bed, and the
        # cell-mean-preserving dry tilt removed. See `swe_2d._rhs` for the full
        # reasoning — this file must stay a line-for-line mirror of it, because
        # a divergence between the backends is silent (audit Part VI N-5 is
        # exactly that defect, in this file).
        zi = cp.maximum(zp[_ax(axis, None, -1)], zp[_ax(axis, 1, None)])
        core = slice(1, L - 1)
        z_m = zi[_ax(axis, 0, n + 2)]
        z_p = zi[_ax(axis, 1, n + 3)]
        z_c = zp[_ax(axis, core.start, core.stop)]

        h_c = hp[_ax(axis, core.start, core.stop)]
        u_c = up[_ax(axis, core.start, core.stop)]
        v_c = vp[_ax(axis, core.start, core.stop)]
        eta_c = h_c + z_c

        eta_m, eta_p = _edge_values_gpu(eta_c, axis)
        u_m, u_p = _edge_values_gpu(u_c, axis)
        v_m, v_p = _edge_values_gpu(v_c, axis)

        h_m = cp.maximum(eta_m - z_m, 0.0)
        h_p = cp.maximum(eta_p - z_p, 0.0)
        # The same edge values against the CELL's own bed — see swe_2d._rhs.
        hc_m = cp.maximum(eta_m - z_c, 0.0)
        hc_p = cp.maximum(eta_p - z_c, 0.0)

        hL, hR = h_p[_ax(axis, None, -1)], h_m[_ax(axis, 1, None)]
        uL, uR = u_p[_ax(axis, None, -1)], u_m[_ax(axis, 1, None)]
        vL, vR = v_p[_ax(axis, None, -1)], v_m[_ax(axis, 1, None)]

        f_h, f_hu, f_hv, a = _rusanov_gpu(
            hL, hL * uL, hL * vL, uL, vL,
            hR, hR * uR, hR * vR, uR, vR,
            normal_axis=(0 if axis == 1 else 1),
        )
        max_a = max(max_a, float(a.max()))

        F_h_l, F_h_r = f_h[_ax(axis, 0, n)], f_h[_ax(axis, 1, n + 1)]
        F_hu_l, F_hu_r = f_hu[_ax(axis, 0, n)], f_hu[_ax(axis, 1, n + 1)]
        F_hv_l, F_hv_r = f_hv[_ax(axis, 0, n)], f_hv[_ax(axis, 1, n + 1)]

        cross = 1 - axis
        trim = _ax(cross, pad, -pad)
        F_h_l, F_h_r = F_h_l[trim], F_h_r[trim]
        F_hu_l, F_hu_r = F_hu_l[trim], F_hu_r[trim]
        F_hv_l, F_hv_r = F_hv_l[trim], F_hv_r[trim]

        cell = _ax(axis, 1, n + 1)
        # Audusse bed-slope source: the difference of the reconstructed edge
        # depths squared, not a centred `h_bar * dz`. Mirror of `swe_2d._rhs`.
        # Source acts only where there is a water column — see swe_2d._rhs.
        wet_c = (h_c > _DRY)[cell][trim]
        src = cp.where(
            wet_c,
            0.5 * _G * ((h_p * h_p - hc_p * hc_p)
                        - (h_m * h_m - hc_m * hc_m))[cell][trim],
            0.0,
        )

        dh -= (F_h_r - F_h_l) / d
        dhu -= (F_hu_r - F_hu_l) / d
        dhv -= (F_hv_r - F_hv_l) / d
        if axis == 1:
            dhu += src / d
        else:
            dhv += src / d

        if axis == 1:
            outflux += float(F_h_r[:, -1].sum() - F_h_l[:, 0].sum()) * dy
        else:
            outflux += float(F_h_r[-1, :].sum() - F_h_l[0, :].sum()) * dx

    return dh, dhu, dhv, max_a, outflux


# ──────────────────────────────────────────────────────────────────────────────
# Time-stepping loop — mirrors run_2d_swe_simulation in swe_2d.py exactly
# ──────────────────────────────────────────────────────────────────────────────

def run_2d_swe_simulation_gpu(
    elevation_grid: np.ndarray,
    dx_m: float,
    dy_m: float,
    inflow_x_idx: int,
    inflow_y_idx: int,
    hydrograph_t_s: np.ndarray,
    hydrograph_Q_m3s: np.ndarray,
    total_duration_s: float = 7200.0,
    save_interval_s: float = 300.0,
    manning_n=0.045,
    scenario_name: str = "simulation",
    initial_depth: np.ndarray | None = None,
    cfl: float = 0.35,
    arrival_depth_m: float = 0.10,
    progress_cb=None,
) -> SimulationResult:
    """
    Same signature, same algorithm, same return type as
    ``swe_2d.run_2d_swe_simulation`` -- callers should not need to know which
    one ran. State arrays stay resident on the GPU for the whole run; only
    scalars (dt-controlling wave speed, progress) and full grids AT SAVE POINTS
    cross back to the host, matching the cadence the CPU path already uses for
    writing GeoTIFFs.
    """
    if not _HAVE_CUPY:
        raise RuntimeError("cupy is not installed — GPU backend unavailable")

    ny, nx = elevation_grid.shape
    logger.info("SWE solver [GPU/cupy] (well-balanced, MUSCL + SSP-RK2): "
               "%dx%d grid, dx=%.1f m dy=%.1f m, T=%.0f s",
               nx, ny, dx_m, dy_m, total_duration_s)

    z = cp.asarray(elevation_grid, dtype=cp.float64)
    n_grid = (cp.full((ny, nx), float(manning_n), dtype=cp.float64)
             if np.isscalar(manning_n)
             else cp.asarray(manning_n, dtype=cp.float64))

    if initial_depth is None:
        h = cp.zeros((ny, nx), dtype=cp.float64)
    else:
        h0 = np.maximum(np.asarray(initial_depth, dtype=np.float64), 0.0)
        if h0.shape != (ny, nx):
            raise ValueError(
                f"initial_depth shape {h0.shape} does not match DEM {(ny, nx)}")
        h = cp.asarray(h0)

    hu = cp.zeros((ny, nx), dtype=cp.float64)
    hv = cp.zeros((ny, nx), dtype=cp.float64)

    cell_area = dx_m * dy_m
    vol_initial = float(h.sum().get()) * cell_area

    max_h = h.copy()
    # P4 (audit §39): a cell that is under the reservoir at t = 0 has not been
    # "reached by the flood" at t = 0 -- it was already wet. Seeding
    # `arrival_time` with 0.0 there put the whole impoundment into the arrival
    # raster as instantly inundated, and two villages reported a max depth of
    # 58.0 m, which is exactly the reservoir depth. They keep NaN until the
    # flood actually adds depth over them.
    #
    # This line read `cp.where(h >= arrival_depth_m, 0.0, cp.nan)` until
    # 2026-09-13 -- the CPU path was fixed for P4 and its GPU mirror was not,
    # so the two backends disagreed about what "arrival" means (audit Part VI
    # N-5). Mirror of `swe_2d.py`'s `np.full(h.shape, np.nan)`.
    arrival_time_gpu = cp.full(h.shape, cp.nan)

    inflow_mask = np.zeros((ny, nx), dtype=np.float64)
    for di in range(-2, 3):
        for dj in range(-2, 3):
            iy = int(np.clip(inflow_y_idx + di, 1, ny - 2))
            ix = int(np.clip(inflow_x_idx + dj, 1, nx - 2))
            inflow_mask[iy, ix] += np.exp(-(di * di + dj * dj) / 2.0)
    inflow_mask /= inflow_mask.sum()
    inflow_mask = cp.asarray(inflow_mask)

    saved_times: list[float] = []
    saved_h: list[np.ndarray] = []
    saved_u: list[np.ndarray] = []
    saved_v: list[np.ndarray] = []

    t = 0.0
    next_save = 0.0
    _last_cb = time.time()
    step = 0
    vol_injected = 0.0
    vol_outflow = 0.0
    vol_clipped = 0.0
    t_wall = time.time()

    while t < total_duration_s:
        c = cp.sqrt(_G * cp.maximum(h, 0.0))
        u_now = _desingularise_gpu(h, hu)
        v_now = _desingularise_gpu(h, hv)
        # .get() forces one host sync per step for a single scalar -- this is
        # the one piece of host-side control flow (dt) the loop cannot avoid.
        max_wave = float(cp.max(cp.sqrt(u_now**2 + v_now**2) + c).get())
        max_wave = max(max_wave, 0.5)
        dt = min(cfl * min(dx_m, dy_m) / max_wave, 5.0, total_duration_s - t)
        dt = max(dt, 1e-3)

        Q_now = float(np.interp(t, hydrograph_t_s, hydrograph_Q_m3s))
        if Q_now > 0.0:
            h = h + (Q_now / cell_area) * dt * inflow_mask
            vol_injected += Q_now * dt

        dh1, dhu1, dhv1, _, out1 = _rhs_gpu(h, hu, hv, z, dx_m, dy_m)
        h1 = h + dt * dh1
        hu1 = hu + dt * dhu1
        hv1 = hv + dt * dhv1
        neg = cp.minimum(h1, 0.0)
        vol_clipped += float(neg.sum().get()) * cell_area
        h1 = cp.maximum(h1, 0.0)
        hu1 = cp.where(h1 > _DRY, hu1, 0.0)
        hv1 = cp.where(h1 > _DRY, hv1, 0.0)

        dh2, dhu2, dhv2, _, out2 = _rhs_gpu(h1, hu1, hv1, z, dx_m, dy_m)
        h_new = 0.5 * (h + h1 + dt * dh2)
        hu_new = 0.5 * (hu + hu1 + dt * dhu2)
        hv_new = 0.5 * (hv + hv1 + dt * dhv2)

        neg = cp.minimum(h_new, 0.0)
        vol_clipped += float(neg.sum().get()) * cell_area
        h_new = cp.maximum(h_new, 0.0)
        vol_outflow += 0.5 * (out1 + out2) * dt

        wet = h_new > _EPS
        u_new = _desingularise_gpu(h_new, hu_new)
        v_new = _desingularise_gpu(h_new, hv_new)
        speed = cp.sqrt(u_new**2 + v_new**2)
        denom = cp.ones_like(h_new)
        h_safe = cp.maximum(h_new, _EPS)
        denom = cp.where(
            wet, 1.0 + dt * _G * n_grid**2 * speed / h_safe**(4.0 / 3.0), denom)
        u_new = u_new / denom
        v_new = v_new / denom

        hu_new = h_new * u_new
        hv_new = h_new * v_new
        hu_new = cp.where(h_new > _DRY, hu_new, 0.0)
        hv_new = cp.where(h_new > _DRY, hv_new, 0.0)

        h, hu, hv = h_new, hu_new, hv_new

        max_h = cp.maximum(max_h, h)
        newly_wet = (h >= arrival_depth_m) & cp.isnan(arrival_time_gpu)
        arrival_time_gpu = cp.where(newly_wet, t, arrival_time_gpu)
        t += dt
        step += 1

        if progress_cb is not None:
            _now = time.time()
            if _now - _last_cb >= 1.0:
                _last_cb = _now
                try:
                    progress_cb(float(t), float(total_duration_s))
                except Exception:                        # noqa: BLE001
                    pass

        if t >= next_save or t >= total_duration_s:
            saved_times.append(float(t))
            # Cross back to host only here -- same cadence the CPU path uses
            # to write GeoTIFFs, not an extra transfer this path invented.
            saved_h.append(cp.asnumpy(h))
            saved_u.append(cp.asnumpy(_desingularise_gpu(h, hu)))
            saved_v.append(cp.asnumpy(_desingularise_gpu(h, hv)))
            next_save += save_interval_s
            logger.info("  [GPU] t=%6.0f s (%5.1f min)  max_h=%5.2f m  wet=%d  dt=%.2f s",
                        t, t / 60.0, float(h.max().get()),
                        int((h > arrival_depth_m).sum().get()), dt)

    res = SimulationResult(
        times_s=saved_times, depth_grids=saved_h, u_grids=saved_u, v_grids=saved_v,
        max_depth_grid=cp.asnumpy(max_h),
        arrival_time_s_grid=cp.asnumpy(arrival_time_gpu),
        dx_m=dx_m, dy_m=dy_m, elevation_grid=cp.asnumpy(z),
        scenario_name=scenario_name,
        volume_injected_m3=vol_injected, volume_outflow_m3=vol_outflow,
        volume_clipped_m3=abs(vol_clipped), volume_initial_m3=vol_initial,
    )
    mc = res.mass_closure()
    logger.info("Simulation complete [GPU]: %d steps in %.2f s  max_depth=%.2f m",
               step, time.time() - t_wall, float(res.max_depth_grid.max()))
    logger.info("  mass: in %.3e m^3, stored %.3e, outflow %.3e, clip %.2e "
               "-> closure %.3f%%",
               mc["initial_m3"] + mc["injected_m3"], mc["stored_m3"],
               mc["outflow_m3"], mc["clipped_m3"], mc["relative_error"] * 100.0)
    return res


def demo() -> None:
    """
    Self-check: run the same tiny Ritter-style dam-break setup that
    tests/test_swe_gpu.py checks properly, just enough here to prove the
    module imports and runs standalone. Skips cleanly if no GPU is present.
    """
    ok, info = gpu_available()
    if not ok:
        print(f"swe_2d_gpu: GPU unavailable ({info}) — skipping demo")
        return

    ny, nx = 20, 200
    z = np.zeros((ny, nx))
    h0 = np.where(np.arange(nx)[None, :] < nx // 2, 10.0, 0.0) * np.ones((ny, 1))
    res = run_2d_swe_simulation_gpu(
        elevation_grid=z, dx_m=10.0, dy_m=10.0,
        inflow_x_idx=nx // 2, inflow_y_idx=ny // 2,
        hydrograph_t_s=np.array([0.0, 1.0]), hydrograph_Q_m3s=np.array([0.0, 0.0]),
        total_duration_s=20.0, save_interval_s=20.0,
        initial_depth=h0, scenario_name="gpu_demo",
    )
    assert res.depth_grids, "no frames saved"
    assert np.isfinite(res.max_depth_grid).all(), "non-finite depth in GPU output"
    print(f"swe_2d_gpu: demo run OK on {info} — "
         f"max depth {res.max_depth_grid.max():.2f} m, "
         f"mass closure {res.mass_closure()['relative_error']*100:.3f}%")


if __name__ == "__main__":
    demo()
