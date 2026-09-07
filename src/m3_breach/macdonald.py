"""
MacDonald & Langemeier (1984) breach parameter model.

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

t_f: not given by this method -> use Froehlich's t_f formula as the estimate,
the same substitution this module already relied on.

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

from . import DamGeometry, BreachParams

_G = 9.81

#: Assumed embankment section, used only to turn M-L's eroded volume into a
#: width. Typical earthfill practice.
_CREST_WIDTH_M = 10.0
_SLOPE_UPSTREAM = 2.5      # Z_u : 1
_SLOPE_DOWNSTREAM = 2.0    # Z_d : 1

#: M-L specify a 0.5H:1V trapezoidal breach.
_SIDE_SLOPE = 0.5


def compute(dam: DamGeometry) -> BreachParams:
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

    # Froehlich t_f as stand-in (method does not define its own)
    t_f = 63.2 * np.sqrt(Vw / (_G * Hw ** 2))
    t_f_h = t_f / 3600.0

    return BreachParams(
        method="MacdonaldLangemeier1984",
        breach_width_m=float(B_avg),
        side_slope_hv=float(_SIDE_SLOPE),
        formation_time_h=float(t_f_h),
        peak_discharge_m3s=float(Q_p),
    )
