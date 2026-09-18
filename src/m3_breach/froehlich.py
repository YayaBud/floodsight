"""
Froehlich (2008) breach parameter model.
CWC (2018) recommends this as the primary method for Indian dams.

Equations
---------
B_avg = 0.27 * k_o * V_w^0.32 * H_w^0.04      [average breach width, m]
t_f   = 63.2 * sqrt(V_w / (g * H_w^2))          [formation time, hours]
Q_p   = 0.607 * V_w^0.295 * H_w^1.24            [peak outflow, m³/s]

Side slope Z:
    overtopping → 1.0 H:1V
    piping      → 0.7 H:1V

k_o:
    overtopping → 1.4
    piping      → 1.0

Reference: Froehlich (2008), Table 2 & Eqs 8-10.
"""

from __future__ import annotations

import numpy as np
from . import DamGeometry, BreachParams, FailureMechanism, require_implemented_mechanism

_G = 9.81  # m/s²


def compute(dam: DamGeometry) -> BreachParams:
    """Return Froehlich (2008) breach parameters for the given dam.

    Froehlich's own equation only distinguishes overtopping vs. piping via
    k_o/Z, and piping is NOT_IMPLEMENTED in this codebase — so both mechanisms
    this codebase implements (overtopping erosion, progressive breach) use the
    "overtopping" branch of Froehlich's coefficients. Callers must call
    require_implemented_mechanism() before reaching here; a NOT_IMPLEMENTED
    mechanism arriving here is a caller bug, not a case this function handles.
    """
    require_implemented_mechanism(dam.failure_mechanism)
    Hw = dam.height_m
    Vw = dam.volume_m3

    # Froehlich (2008) specifies k_o = 1.3 for overtopping. 1.4 is Froehlich
    # (1995a), which pairs with a DIFFERENT regression -- 0.1803 and h_b^0.19,
    # not the 0.27 and H_w^0.04 used below. Carrying 1995a's coefficient into
    # 2008's form mixed two papers; corrected 2026-09-13 against the HEC-RAS 1D
    # Technical Reference, *Estimating Breach Parameters*. Effect on a
    # 58 m / 2.7e7 m^3 dam: B_avg 106.19 m -> 98.60 m.
    k_o = 1.3
    Z   = 1.0

    B_avg = 0.27 * k_o * (Vw ** 0.32) * (Hw ** 0.04)  # m
    t_f   = 63.2 * np.sqrt(Vw / (_G * Hw ** 2))         # s → convert below
    t_f_h = t_f / 3600.0                                 # hours
    Q_p   = 0.607 * (Vw ** 0.295) * (Hw ** 1.24)        # m³/s

    return BreachParams(
        method="Froehlich2008",
        breach_width_m=float(B_avg),
        side_slope_hv=float(Z),
        formation_time_h=float(t_f_h),
        peak_discharge_m3s=float(Q_p),
    )
