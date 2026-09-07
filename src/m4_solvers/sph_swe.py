"""
FloodSight — M4 Lagrangian arm: SWE-SPH (1D)
============================================
Smoothed Particle Hydrodynamics applied to the shallow water equations, which
is the Lagrangian counterpart to the Eulerian finite-volume solver in
``swe_2d.py``. SIH26161 deliverable (i) asks for a Smoothed Particle
Hydrodynamics model and a comparison of scenarios between methods; this is the
SPH arm of that comparison.

Why 1D, and why not full 3D SPH
-------------------------------
Classical 3D SPH is not tractable for this problem. The published scaling is
roughly one million particles for **1.5 seconds** of physical time (~15 min on
a GPU); a 60-minute flood down a 30 km valley is several orders of magnitude
beyond that, which is why large-domain flood work uses depth-averaged
formulations rather than 3D particles.

SWE-SPH is the standard resolution: particles carry depth-integrated mass and
move in the horizontal plane, so the vertical dimension is integrated out
exactly as it is in the finite-volume arm. This module implements the 1D case,
which is what the dam-break benchmark needs and what can be validated against
a closed-form solution. It is deliberately not presented as a general 2D
terrain solver — that claim would not be checkable here.

Why not PySPH
-------------
PySPH (IIT Bombay) is the natural library for this and is cited in the design
notes, but it needs a C compiler toolchain to build its extensions and is not
installed in this environment. Rather than badge an SPH arm that does not run,
the scheme is implemented directly against numpy so the comparison is real.

Formulation
-----------
Depth by kernel summation, which conserves mass exactly because the particle
masses are fixed::

    d_i = sum_j m_j W(x_i - x_j, hsml)

Momentum from the free-surface gradient, using the standard SPH difference
operator (first-order consistent, and it handles the bed slope naturally
because eta carries the bed)::

    eta_i    = d_i + z_i
    du_i/dt  = -g * sum_j (m_j / d_j) (eta_j - eta_i) dW_ij/dx

Friction is applied point-implicitly, matching the Eulerian arm so the two are
compared on equal terms rather than one being quietly better damped.

Kernel: cubic spline (Monaghan), 1D normalisation 2/(3h), compact support 2h.

References
----------
Monaghan, J. J. (1992). Smoothed particle hydrodynamics. ARA&A, 30, 543-574.
Rodriguez-Paz, M., Bonet, J. (2005). A corrected smooth particle hydrodynamics
    formulation of the shallow-water equations. Computers & Structures, 83.
Vacondio, R., Rogers, B. D., Stansby, P. K. (2012). Accurate particle splitting
    for SPH in shallow water with shock capturing. Int. J. Numer. Meth. Fluids.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

_G = 9.81
_DRY = 1e-6      # depth below which a particle carries no momentum


@dataclass
class SPHResult:
    """Final state of an SWE-SPH run."""
    x: np.ndarray            # particle positions [m]
    depth: np.ndarray        # depth at each particle [m]
    u: np.ndarray            # velocity at each particle [m/s]
    t_s: float               # time reached [s]
    n_particles: int
    steps: int
    wall_time_s: float
    mass_total: float        # invariant: fixed by construction

    def sample_on(self, x_query: np.ndarray, hsml: float) -> np.ndarray:
        """
        Interpolate depth onto fixed positions, for comparison with a grid
        solver or an analytical profile.

        Uses the same kernel summation that defines depth inside the scheme, so
        the comparison samples the field the method actually represents rather
        than a nearest-particle approximation.
        """
        d = np.zeros_like(x_query, dtype=float)
        m = self.mass_total / self.n_particles
        for k, xq in enumerate(x_query):
            r = np.abs(self.x - xq)
            near = r < 2.0 * hsml
            if near.any():
                d[k] = np.sum(m * _kernel_w(r[near], hsml))
        return d


def _kernel_w(r: np.ndarray, hsml: float) -> np.ndarray:
    """Monaghan cubic spline W(r, h), 1D normalisation."""
    q = r / hsml
    alpha = 2.0 / (3.0 * hsml)
    w = np.zeros_like(q)
    m1 = q < 1.0
    m2 = (q >= 1.0) & (q < 2.0)
    w[m1] = alpha * (1.0 - 1.5 * q[m1] ** 2 + 0.75 * q[m1] ** 3)
    w[m2] = alpha * 0.25 * (2.0 - q[m2]) ** 3
    return w


def _kernel_dw(dx: np.ndarray, hsml: float) -> np.ndarray:
    """dW/dx for the cubic spline, signed along the separation vector."""
    r = np.abs(dx)
    q = r / hsml
    alpha = 2.0 / (3.0 * hsml)
    dwdq = np.zeros_like(q)
    m1 = (q < 1.0) & (q > 0.0)
    m2 = (q >= 1.0) & (q < 2.0)
    dwdq[m1] = alpha * (-3.0 * q[m1] + 2.25 * q[m1] ** 2)
    dwdq[m2] = alpha * (-0.75 * (2.0 - q[m2]) ** 2)
    # chain rule: dW/dx = dW/dq * (1/h) * sign(dx)
    return dwdq / hsml * np.sign(dx)


def run_swe_sph_1d(
    x0: np.ndarray,
    depth0: np.ndarray,
    bed0: np.ndarray | None = None,
    total_duration_s: float = 30.0,
    hsml_factor: float = 1.5,
    cfl: float = 0.25,
    manning_n: float = 0.0,
    max_steps: int = 200_000,
) -> SPHResult:
    """
    Advance 1D SWE-SPH particles.

    Parameters
    ----------
    x0     : initial particle positions [m], assumed evenly spaced.
    depth0 : initial depth carried by each particle [m].
    bed0   : bed elevation at each particle [m]; flat if omitted. The bed is
             carried by the particle and moves with it, which is an
             approximation valid for the gentle beds used in the benchmark.
    manning_n : Manning roughness. Zero for the frictionless Ritter benchmark.

    Returns
    -------
    :class:`SPHResult` with final positions, depths and velocities.
    """
    x = np.asarray(x0, dtype=float).copy()
    d = np.asarray(depth0, dtype=float).copy()
    z = np.zeros_like(x) if bed0 is None else np.asarray(bed0, dtype=float).copy()
    n = x.size

    dx0 = float(np.median(np.diff(np.sort(x0))))
    hsml = hsml_factor * dx0

    # Particle mass is depth x spacing, fixed for all time. Total mass is then
    # an exact invariant of the scheme rather than something to be checked.
    m = float(np.mean(d[d > 0]) * dx0) if np.any(d > 0) else dx0
    # Give every particle the same mass so the summation is unbiased; particles
    # in the dry region simply start with no neighbours contributing depth.
    masses = np.full(n, m, dtype=float)
    mass_total = float(masses.sum())

    u = np.zeros(n, dtype=float)
    t = 0.0
    step = 0
    t_wall = time.time()

    def depth_from_summation(pos: np.ndarray) -> np.ndarray:
        out = np.zeros(n, dtype=float)
        for i in range(n):
            r = np.abs(pos - pos[i])
            near = r < 2.0 * hsml
            out[i] = np.sum(masses[near] * _kernel_w(r[near], hsml))
        return out

    d = depth_from_summation(x)

    while t < total_duration_s and step < max_steps:
        c = np.sqrt(_G * np.maximum(d, 0.0))
        vmax = float(np.max(np.abs(u) + c)) if n else 1.0
        dt = cfl * hsml / max(vmax, 1e-3)
        dt = min(dt, total_duration_s - t)
        if dt <= 0:
            break

        eta = d + z

        # Free-surface gradient -> acceleration
        accel = np.zeros(n, dtype=float)
        for i in range(n):
            dxi = x[i] - x
            r = np.abs(dxi)
            near = (r < 2.0 * hsml) & (r > 0.0)
            if not near.any():
                continue
            dw = _kernel_dw(dxi[near], hsml)
            dj = np.maximum(d[near], _DRY)
            accel[i] = -_G * np.sum((masses[near] / dj) * (eta[near] - eta[i]) * dw)

        u = u + dt * accel

        # Point-implicit Manning friction, matching the Eulerian arm.
        if manning_n > 0.0:
            wet = d > _DRY
            denom = np.ones_like(u)
            denom[wet] = 1.0 + dt * _G * manning_n**2 * np.abs(u[wet]) / \
                np.maximum(d[wet], 1e-3) ** (4.0 / 3.0)
            u = u / denom

        x = x + dt * u
        d = depth_from_summation(x)

        t += dt
        step += 1

    wall = time.time() - t_wall
    logger.info("SWE-SPH: %d particles, %d steps, t=%.1f s, %.2f s wall, "
                "mass invariant %.4e", n, step, t, wall, mass_total)
    return SPHResult(x=x, depth=d, u=u, t_s=t, n_particles=n, steps=step,
                     wall_time_s=wall, mass_total=mass_total)


def ritter_dam_break_sph(
    h1_m: float = 10.0,
    t_eval_s: float = 30.0,
    domain_m: float = 4000.0,
    particle_spacing_m: float = 10.0,
) -> tuple[SPHResult, float]:
    """
    Set up and run the classical dam break with SWE-SPH.

    Reservoir of depth ``h1_m`` occupies x < 0; downstream is dry. Returns the
    result and the smoothing length used, which the caller needs in order to
    sample the particle field for comparison.
    """
    n_res = int((domain_m / 2.0) / particle_spacing_m)
    # Particles fill the reservoir only; the dry bed downstream has no mass to
    # carry, and the flow reaches it by particles moving into it.
    x0 = np.linspace(-domain_m / 2.0, 0.0, n_res)
    d0 = np.full(n_res, h1_m)

    res = run_swe_sph_1d(
        x0=x0, depth0=d0, bed0=None,
        total_duration_s=t_eval_s, manning_n=0.0,
    )
    hsml = 1.5 * float(np.median(np.diff(np.sort(x0))))
    return res, hsml
