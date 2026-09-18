"""
MacDonald & Langridge-Monopolis (1984) breach parameter model.

NAME: the second author is Langridge-Monopolis, not "Langemeier". The wrong
name was carried in this module's title, in its `method=` string and in
`m3_breach/__init__.py`'s reference list until 2026-09-13, verified against the
HEC-RAS 1D Technical Reference, *Estimating Breach Parameters*.

Equations
---------
Peak outflow, from the breach formation factor V_w * H_w:

    Q_p = 1.154 * (V_w * H_w)^0.412         [earthfill]

Breach size, from the volume of embankment material the flood erodes:

    V_eroded = 0.0261 * (V_w * H_w)^0.769   [earthfill]

M-L specify a trapezoidal breach with 0.5H:1V side slopes cut through the full
height of the embankment. Treating that breach as a prism driven through the
dam gives the average width directly:

    V_eroded = B_avg * H_d * W_mean
    -> B_avg  = V_eroded / (H_d * W_mean)

where W_mean is the mean thickness of the embankment in the flow direction. For
a trapezoidal dam of crest width W_c and face slopes Z_u:1 and Z_d:1,

    W_mean = W_c + (Z_u + Z_d) * H_d / 2

t_f: M-LM DO define a formation time, contrary to what this module asserted
until 2026-09-13. It is

    t_f = 0.0179 * V_eroded^0.364            [hours]

and it is used. Froehlich's t_f was standing in for it, which made two of the
three ensemble arms report the same formation time and collapsed part of the
spread the ensemble exists to represent.

Why this replaced a weir back-calculation
-----------------------------------------
B_avg used to be recovered from Q_p through a sharp-crested weir relation,

    B_avg = Q_p / ((2/3) * C_d * sqrt(2g) * H_w^1.5)

which mixes two incompatible quantities: Q_p is a *reservoir-drawdown-limited*
peak from a regression, while the weir relation is an *instantaneous* discharge
for a fully-formed opening under steady head. Dividing one by the other gave
6.2 m of breach width for the 73 m Derna embankment, against 101 m from
Froehlich and 242 m from von Thun. That was not a cosmetic error:
``breach_width_m`` feeds route_breach(), so it routed one of the three ensemble
arms through a breach an order of magnitude too narrow. The eroded-volume
equation is M-L's own and is dimensionally the right tool for a breach size.

The embankment cross-section is not carried on DamGeometry, so it is assumed.
The values are ordinary earthfill practice and are stated here rather than
buried; a scenario that knows its dam better should carry its own numbers.

Reference: MacDonald & Langemeier (1984), ASCE JHE 110(5).
"""

from __future__ import annotations

import numpy as np

from . import DamGeometry, BreachParams, require_implemented_mechanism

_G = 9.81

#: Assumed embankment section, used only to turn M-L's eroded volume into a
#: width. Typical earthfill practice.
_CREST_WIDTH_M = 10.0
_SLOPE_UPSTREAM = 2.5      # Z_u : 1
_SLOPE_DOWNSTREAM = 2.0    # Z_d : 1

#: M-L specify a 0.5H:1V trapezoidal breach.
_SIDE_SLOPE = 0.5


def compute(dam: DamGeometry) -> BreachParams:
    """Return MacDonald & Langridge-Monopolis (1984) breach parameters.

    Callers must have gated the mechanism; this checks anyway, because
    froehlich.py and von_thun.py both do and an unimplemented mechanism
    reaching only this one of the three would be silent.
    """
    require_implemented_mechanism(dam.failure_mechanism)
    Hw = dam.height_m
    Vw = dam.volume_m3

    # Erosion cuts through the embankment, so the dam height governs the breach
    # prism. Fall back to the water height when the dam height is unset, and
    # never let it be the smaller of the two.
    Hd = max(float(getattr(dam, "dam_height_m", 0.0) or 0.0), float(Hw))

    formation_factor = Vw * Hw

    Q_p = 1.154 * (formation_factor ** 0.412)
    V_eroded = 0.0261 * (formation_factor ** 0.769)

    w_mean = _CREST_WIDTH_M + (_SLOPE_UPSTREAM + _SLOPE_DOWNSTREAM) * Hd / 2.0
    B_avg = V_eroded / (Hd * w_mean)

    # M-LM's OWN formation time, in hours, from the eroded embankment volume.
    # This replaced a Froehlich t_f stand-in on 2026-09-13; for a 58 m / 2.7e7 m^3
    # dam it moves t_f from 0.502 h to 1.779 h.
    t_f_h = 0.0179 * (V_eroded ** 0.364)

    return BreachParams(
        method="MacDonaldLangridgeMonopolis1984",
        breach_width_m=float(B_avg),
        side_slope_hv=float(_SIDE_SLOPE),
        formation_time_h=float(t_f_h),
        peak_discharge_m3s=float(Q_p),
    )
