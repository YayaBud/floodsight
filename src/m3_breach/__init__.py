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
from enum import Enum
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────────
# Failure mechanism taxonomy
# ──────────────────────────────────────────────────────────────────────────────
# Authoritative source: FLOODSIGHT_DEEP_REVIEW_2026-09-11.md, section
# "Mechanism -> implementation status" (13-row table). Every row has a member
# here. Only OVERTOPPING_EROSION and PROGRESSIVE_BREACH are implemented; every
# other value must raise via require_implemented_mechanism() rather than
# silently running overtopping/progressive-breach physics under a different
# name.

class FailureMechanism(str, Enum):
    OVERTOPPING_EROSION = "overtopping_erosion"
    PROGRESSIVE_BREACH = "progressive_breach"
    SUDDEN_RAPID_BREACH = "sudden_rapid_breach"                        # NOT_IMPLEMENTED
    PARTIAL_BREACH = "partial_breach"                                  # NOT_IMPLEMENTED
    PIPING = "piping"                                                  # NOT_IMPLEMENTED
    FOUNDATION_FAILURE = "foundation_failure"                          # NOT_IMPLEMENTED
    STRUCTURAL_CONCRETE_FAILURE = "structural_concrete_failure"        # NOT_IMPLEMENTED
    GATE_FAILURE = "gate_failure"                                      # NOT_IMPLEMENTED
    SPILLWAY_CAPACITY_FAILURE = "spillway_capacity_failure"            # NOT_IMPLEMENTED
    LANDSLIDE_INDUCED_OVERTOPPING = "landslide_induced_overtopping"    # NOT_IMPLEMENTED
    EARTHQUAKE_INDUCED_FAILURE = "earthquake_induced_failure"          # NOT_IMPLEMENTED
    NATURAL_LANDSLIDE_DAM_FAILURE = "natural_landslide_dam_failure"    # NOT_IMPLEMENTED
    MORAINE_GLOF_OUTBURST = "moraine_glof_outburst"                    # NOT_IMPLEMENTED
    RIVER_BLOCKAGE_OUTBURST = "river_blockage_outburst"                # NOT_IMPLEMENTED

    def __str__(self) -> str:            # so json.dump and f-strings stay clean
        return self.value


#: The only two mechanisms with any physics behind them today. Every other
#: enum member is NOT_IMPLEMENTED per the deep-review's table and must raise.
_IMPLEMENTED_MECHANISMS = {FailureMechanism.OVERTOPPING_EROSION, FailureMechanism.PROGRESSIVE_BREACH}


def require_implemented_mechanism(mechanism: "FailureMechanism") -> None:
    """Raise NotImplementedError unless `mechanism` is one of the two
    mechanisms this codebase actually has physics for. Callers must call this
    before building breach params for a mechanism-bearing DamGeometry/cascade
    run, so an unimplemented mechanism fails loudly instead of silently
    reusing overtopping physics under a different label."""
    if mechanism not in _IMPLEMENTED_MECHANISMS:
        raise NotImplementedError(
            f"failure mechanism {mechanism.value!r} is NOT_IMPLEMENTED — "
            f"see FLOODSIGHT_DEEP_REVIEW_2026-09-11.md's mechanism status table"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Flow regime — which physics class an event belongs to
# ──────────────────────────────────────────────────────────────────────────────
# The 2D solver integrates the clear-water shallow-water equations: constant
# density (rho cancels out of every term and appears nowhere in swe_2d.py),
# Newtonian resistance through a single Manning n, a fixed bed, and no solid
# phase. That is the correct model for one class of event and the wrong model
# for others, and the difference is not a coefficient — it is extra equations
# and extra state.
#
# Nothing in this repository models sediment or debris. Verified by search:
# `rho`, concentration, entrainment, rheology, yield stress, Bingham, Voellmy,
# scour, deposition and bulking appear nowhere in any solver. The only hits are
# the words "debris"/"avalanche" inside scenario metadata strings.
#
# So a debris-flow event cannot be represented by raising Q. It must be
# declared, and a run that is outside the solver's envelope must say so rather
# than present a clear-water answer as the event.

class FlowRegime(str, Enum):
    #: Newtonian, constant density, fixed bed. What swe_2d.py solves.
    CLEAR_WATER = "clear_water"
    #: Volumetric solids concentration roughly 0.05-0.20. Still Newtonian
    #: enough for SWE, but density and resistance are elevated and the bed is
    #: mobile. Modelled here as clear water; the bias is stated, not hidden.
    HYPERCONCENTRATED = "hyperconcentrated"
    #: Volumetric solids concentration roughly 0.4-0.8. Non-Newtonian, density
    #: 1.8-2.3x water, resistance dominated by grain collision and yield
    #: stress, mass gained by entrainment along the path. SWE cannot represent
    #: this, and no amount of Q or n makes it able to.
    DEBRIS_FLOW = "debris_flow"

    def __str__(self) -> str:
        return self.value


#: Regimes the clear-water solver may be used for without qualification.
_SWE_APPLICABLE_REGIMES = {FlowRegime.CLEAR_WATER}

#: Regimes where SWE is a documented approximation rather than the governing
#: model: the run proceeds, the caveat is carried into the manifest.
_SWE_APPROXIMATE_REGIMES = {FlowRegime.HYPERCONCENTRATED}

#: State variables and closures a debris-flow run would need, which this
#: codebase has none of. Kept next to the enum so the gap is specified rather
#: than gestured at.
DEBRIS_FLOW_MISSING_PHYSICS = (
    "solid volumetric concentration c(x,y,t) as a transported state variable",
    "mixture density rho_m = rho_w (1 - c) + rho_s c, which no term in swe_2d.py carries",
    "non-Newtonian resistance closure (Voellmy mu/xi, or Bingham/Herschel-Bulkley "
    "yield stress tau_y + plastic viscosity) replacing the single Manning n",
    "bed entrainment/deposition law E(x,y,t) coupling depth-averaged mass to a "
    "mobile bed elevation, i.e. dz_b/dt != 0",
    "an Exner-type bed-evolution equation so scour and deposition change the terrain "
    "the flow is routed over",
    "grain-size-dependent settling and phase separation for the coarse fraction",
    "a momentum equation whose pressure term uses rho_m, not rho_w",
    "impact pressure on structures (rho_m v^2), which exposure currently proxies "
    "from depth alone",
)


def regime_status(regime: "FlowRegime") -> dict:
    """Classify a run's flow regime against what the solver can represent."""
    applicable = regime in _SWE_APPLICABLE_REGIMES
    approximate = regime in _SWE_APPROXIMATE_REGIMES
    return {
        "flow_regime": str(regime),
        "solver": "clear_water_swe",
        "applicable": bool(applicable),
        "approximate": bool(approximate),
        "missing_physics": [] if applicable else list(DEBRIS_FLOW_MISSING_PHYSICS),
        "note": (
            "clear-water SWE is the governing model for this event" if applicable
            else "solids raise density and resistance; extent and depth are "
                 "modelled as clear water and read low, momentum reads low"
            if approximate
            else "clear-water SWE is NOT the governing model for this event — "
                 "the result is a water-only analogue, not the event"
        ),
    }


def parse_flow_regime(value: str | None) -> "FlowRegime":
    """Scenario config string to FlowRegime. Unset means clear water."""
    if value is None:
        return FlowRegime.CLEAR_WATER
    try:
        return FlowRegime(value)
    except ValueError:
        raise ValueError(
            f"unrecognized flow_regime {value!r}; expected one of "
            f"{[r.value for r in FlowRegime]}"
        ) from None


#: Maps the API/CLI's accepted `failure_mode` strings (src/api/main.py's
#: RunRequest.failure_mode / run_pipeline.py's execute_full_simulation
#: failure_mode param — {"overtopping", "piping", "instantaneous", "breach"})
#: to FailureMechanism members. "instantaneous" maps to SUDDEN_RAPID_BREACH
#: (the closest named mechanism); "breach" is deliberately NOT mapped here —
#: it is too vague to map safely to a specific mechanism (it could mean
#: overtopping erosion, progressive breach, or something else entirely) and
#: mapping it to a guessed default would risk silently running one
#: mechanism's physics under an unrelated label. Callers passing "breach"
#: must raise a clear ValueError instead — see failure_mode_to_mechanism.
_FAILURE_MODE_TO_MECHANISM = {
    "overtopping": FailureMechanism.OVERTOPPING_EROSION,
    "piping": FailureMechanism.PIPING,
    "instantaneous": FailureMechanism.SUDDEN_RAPID_BREACH,
}


def failure_mode_to_mechanism(failure_mode: str) -> "FailureMechanism":
    """Convert an API/CLI failure_mode string to a FailureMechanism member.

    Raises ValueError for "breach" (too ambiguous to map safely — could mean
    overtopping erosion or progressive breach or something else; guessing
    would risk silently substituting one mechanism's physics for another) and
    for any other unrecognized string.
    """
    if failure_mode == "breach":
        raise ValueError(
            "failure_mode 'breach' is too ambiguous to map to a single "
            "FailureMechanism — specify 'overtopping', 'piping', or "
            "'instantaneous' instead"
        )
    try:
        return _FAILURE_MODE_TO_MECHANISM[failure_mode]
    except KeyError:
        raise ValueError(f"unrecognized failure_mode: {failure_mode!r}") from None


# ──────────────────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class DamGeometry:
    """Physical properties of the dam / impoundment."""
    height_m: float          # H_w — height of water above breach invert [m]
    volume_m3: float         # V_w — volume of water stored at failure [m³]
    dam_height_m: float      # H_d — total dam height [m]
    failure_mechanism: FailureMechanism = FailureMechanism.OVERTOPPING_EROSION
    crest_length_m: Optional[float] = None          # Engineering crest length (clamps breach width)
    dam_type: Optional[str] = None                  # "earthfill", "rockfill", "concrete", "masonry"
    # `spillway_capacity_m3s` was removed: it travelled from the API request
    # through the pipeline into this dataclass and was read by nothing. The
    # cascade path routes a real spillway from its own config
    # (`cascade["spillway"]`); this field only ever looked like it did.


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
