"""
FloodSight — M3 Breach Ensemble
================================
Froehlich (2008) breach parameter equations as recommended by CWC (2018) for
Indian dams. Two additional methods (Von Thun & Gillette 1990; MacDonald &
Langemeier 1984) are included as ensemble arms.

All equations are dimensionally explicit (SI throughout).

References
----------
Froehlich, D. C. (2008). Embankment dam breach parameters and their
    uncertainties. Journal of Hydraulic Engineering, 134(12), 1708–1721.
Von Thun, J. L. & Gillette, D. R. (1990). Guidance on breach parameters.
    Unpublished internal document, US Bureau of Reclamation.
MacDonald, T. C. & Langemeier, J. (1984). Breaching characteristics of dam
    failures. Journal of Hydraulic Engineering, 110(5), 567–586.
CWC (2018). Guidelines for Mapping Flood Risks Associated with Dams.
    Central Water Commission, New Delhi.
"""

from __future__ import annotations

import numpy as np
from dataclasses import dataclass
from typing import Literal


# ──────────────────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class DamGeometry:
    """Physical properties of the dam / impoundment."""
    height_m: float          # H_w — height of water above breach invert [m]
    volume_m3: float         # V_w — volume of water stored at failure [m³]
    dam_height_m: float      # H_d — total dam height [m]
    failure_mode: Literal["overtopping", "piping"] = "overtopping"
    crest_length_m: Optional[float] = None          # Engineering crest length (clamps breach width)
    dam_type: Optional[str] = None                  # "earthfill", "rockfill", "concrete", "masonry"
    spillway_capacity_m3s: Optional[float] = None   # Controlled spillway discharge capacity


@dataclass
class BreachParams:
    """Breach geometry and timing from one method."""
    method: str
    breach_width_m: float         # B_avg — average breach width [m]
    side_slope_hv: float          # Z — side slope (H:V)
    formation_time_h: float       # t_f — breach formation time [hours]
    peak_discharge_m3s: float     # Q_p — peak outflow [m³/s]


@dataclass
class BreachEnsemble:
    """Three-arm ensemble: pessimistic / central / optimistic."""
    pessimistic: BreachParams
    central: BreachParams
    optimistic: BreachParams
    dam: DamGeometry
