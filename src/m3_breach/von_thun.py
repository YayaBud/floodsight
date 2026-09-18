"""
Von Thun & Gillette (1990) breach parameter model.

Equations
---------
B_avg = 2.5 * H_w + C_b        [average breach width, m]

C_b depends on reservoir storage:
    V_w <  1.23 Mm³  → C_b = 6.1
    V_w <  6.17 Mm³  → C_b = 18.3
    V_w < 12.35 Mm³  → C_b = 42.7
    V_w >= 12.35 Mm³ → C_b = 54.9

t_f: VT&G give two alternatives, distinguished by EMBANKMENT ERODIBILITY, not by
     failure mode:
    erosion-resistant → t_f = (B_avg / 4) / H_w          [hours]   <-- implemented
    easily erodible   → t_f = B_avg / (4 * H_w + 61)     [hours]

     A "piping" branch t_f = (B_avg / 4) / (1.4 * H_w) was documented here until
     2026-09-13 and appears in no source. It has been removed. The implemented
     form is the erosion-resistant alternative and is labelled as such.

Q_p: VT&G define B_avg and t_f but NOT a peak discharge. What `compute()`
     actually returns is **Froehlich (2008)'s** Q_p regression, verbatim:
         Q_p = 0.607 * V_w^0.295 * H_w^1.24
     That is a borrow, it is stated in `compute()`, and it means this method's
     Q_p is bit-identical to froehlich.py's for the same dam. Do not read the
     three ensemble arms as three independent peak-discharge estimates: there
     are two. This docstring previously documented a weir rearrangement
         Q_p ~ (1/3)(B_avg + Z*H_w)*sqrt(2g)*H_w^1.5
     that the code does not compute.

Side slope Z: 1.0 H:1V (overtopping or piping — not distinguished in original)

Reference: Von Thun & Gillette (1990) USBR internal document.
"""

from __future__ import annotations

import numpy as np
from . import DamGeometry, BreachParams, FailureMechanism, require_implemented_mechanism

_G = 9.81


def _cb(volume_m3: float) -> float:
    V_Mm3 = volume_m3 / 1e6
    if V_Mm3 < 1.23:
        return 6.1
    elif V_Mm3 < 6.17:
        return 18.3
    elif V_Mm3 < 12.35:
        return 42.7
    else:
        return 54.9


def compute(dam: DamGeometry) -> BreachParams:
    """Return Von Thun & Gillette (1990) breach parameters.

    Same reasoning as froehlich.py: this method's overtopping/piping branch
    only matters for piping, which is NOT_IMPLEMENTED here, so every mechanism
    that reaches this function uses the overtopping branch. Callers must call
    require_implemented_mechanism() before reaching here.
    """
    require_implemented_mechanism(dam.failure_mechanism)
    Hw = dam.height_m
    Vw = dam.volume_m3
    Z  = 1.0

    B_avg = 2.5 * Hw + _cb(Vw)  # m

    t_f_h = (B_avg / 4.0) / Hw

    # von Thun & Gillette define B_avg and t_f but NOT a peak discharge, so
    # Q_p is borrowed from Froehlich's regression -- the same pattern by which
    # macdonald.py borrows Froehlich's t_f.
    #
    # It previously used a weir rearrangement,
    #     Q_p = (1/3)(B_avg + Z*Hw) * sqrt(2g) * Hw^1.5,
    # which returns an INSTANTANEOUS discharge for a fully-formed opening and
    # knows nothing about the reservoir behind it. For Derna (Hw = 75 m,
    # B_avg = 242 m) that gave 304,000 m^3/s -- a rate that would empty the
    # whole 22.5 Mm^3 impoundment in 84 seconds. The regression forms are
    # drawdown-limited by construction and are the right family of estimate
    # here. Routing is unaffected either way: route_breach() reads B_avg, Z and
    # t_f, never Q_p.
    Q_p = 0.607 * (Vw ** 0.295) * (Hw ** 1.24)

    return BreachParams(
        method="VonThunGillette1990",
        breach_width_m=float(B_avg),
        side_slope_hv=float(Z),
        formation_time_h=float(t_f_h),
        peak_discharge_m3s=float(Q_p),
    )
