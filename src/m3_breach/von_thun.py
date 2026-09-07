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

t_f:
    overtopping → t_f = (B_avg / 4) / H_w      [hours]
    piping      → t_f = (B_avg / 4) / (1.4 * H_w)  [hours]

Q_p: Not directly given by this method → use SCS-style rearrangement of breach
     geometry + H_w:
    Q_p ≈ (1/3) * (B_avg + Z * H_w) * sqrt(2 * g) * H_w^1.5

Side slope Z: 1.0 H:1V (overtopping or piping — not distinguished in original)

Reference: Von Thun & Gillette (1990) USBR internal document.
"""

from __future__ import annotations

import numpy as np
from . import DamGeometry, BreachParams

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
    Hw = dam.height_m
    Vw = dam.volume_m3
    Z  = 1.0

    B_avg = 2.5 * Hw + _cb(Vw)  # m

    if dam.failure_mode == "overtopping":
        t_f_h = (B_avg / 4.0) / Hw
    else:
        t_f_h = (B_avg / 4.0) / (1.4 * Hw)

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
