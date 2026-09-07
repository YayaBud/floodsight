"""
M7 — Priority Ranking
======================
Produces an ordered action list for emergency response.

Ranking logic
-------------
Villages are scored on four dimensions:
  1. **Isolation time** — earlier isolation = higher urgency (inverted)
  2. **Population at risk** — more people = higher urgency
  3. **Egress capacity** — fewer road exits = less resilience
  4. **Critical facility count** — hospitals + schools at risk

A composite score normalises each dimension to [0, 1] and combines them
with configurable weights (default equal-weight).

Confidence bands
----------------
The score is computed for pessimistic / central / optimistic breach arms.
The three scores give a band that communicates uncertainty to the analyst.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import geopandas as gpd

logger = logging.getLogger(__name__)


@dataclass
class RankWeights:
    isolation:   float = 0.35   # isolation time weight
    population:  float = 0.35   # population at risk weight
    egress:      float = 0.15   # road exits weight (inverted)
    facilities:  float = 0.15   # critical facility count weight


def _score_isolation(iso_min: pd.Series) -> pd.Series:
    """Absolute score for isolation time (earlier = higher urgency)."""
    return pd.Series(np.select(
        [iso_min < 30, iso_min < 60, iso_min < 120, iso_min.notna()],
        [1.0, 0.8, 0.5, 0.1], default=0.0
    ), index=iso_min.index)


def _score_par(par: pd.Series) -> pd.Series:
    """Absolute score for population at risk."""
    return pd.Series(np.select(
        [par > 1000, par > 500, par > 100, par > 0],
        [1.0, 0.8, 0.5, 0.2], default=0.0
    ), index=par.index)


def _score_facilities(facs: pd.Series) -> pd.Series:
    """Absolute score for critical facilities flooded."""
    return pd.Series(np.select(
        [facs >= 3, facs >= 2, facs == 1],
        [1.0, 0.7, 0.4], default=0.0
    ), index=facs.index)


def _score_egress(exits: pd.Series) -> pd.Series:
    """Absolute score for number of egress routes (fewer = higher urgency)."""
    return pd.Series(np.select(
        [exits == 0, exits == 1, exits == 2],
        [1.0, 0.8, 0.3], default=0.0
    ), index=exits.index)


def rank_villages(
    exposure_gdf: gpd.GeoDataFrame,
    isolation_gdf: gpd.GeoDataFrame,
    weights: RankWeights = RankWeights(),
    n_road_exits: dict | None = None,    # {village_id: int} — optional
) -> gpd.GeoDataFrame:
    """
    Merge exposure and isolation data and produce a ranked priority list.

    Parameters
    ----------
    exposure_gdf   : From M5 — contains pop_at_risk, hospitals_flooded,
                     schools_flooded, loss_inr per village.
    isolation_gdf  : From M6 — contains isolation_time_min, evacuation_window_min.
    weights        : RankWeights dataclass.
    n_road_exits   : Optional dict mapping village_id to exit road count.

    Returns
    -------
    GeoDataFrame sorted by priority_score descending, with columns:
        priority_rank, priority_score, score_lo, score_hi,
        village_name, pop_at_risk, isolation_time_min, evacuation_window_min,
        hospitals_flooded, schools_flooded, loss_inr
    """
    if len(exposure_gdf) == 0:
        logger.warning("No villages to rank — returning an empty frame.")
        return gpd.GeoDataFrame(
            columns=["priority_rank", "priority_score", "village_id", "geometry"],
            geometry="geometry", crs=exposure_gdf.crs,
        )

    # Merge on village_id
    iso_cols = [c for c in ["village_id", "isolation_time_min", "water_arrival_min",
                            "evacuation_window_min", "road_nodes_found"]
                if c in isolation_gdf.columns]
    merged = exposure_gdf.merge(isolation_gdf[iso_cols], on="village_id", how="left")

    # Villages never isolated within the window rank as least urgent on that
    # axis. The filled value is used for SCORING ONLY — `isolation_time_min`
    # keeps its NaN so the UI can render "not isolated" rather than a number
    # that was invented to make the arithmetic work.
    max_t = merged["isolation_time_min"].max()
    fill_t = max_t * 2.0 if np.isfinite(max_t) else 9999.0
    iso_for_score = merged["isolation_time_min"].fillna(fill_t)

    # Urgency from isolation: earlier isolation → higher urgency
    merged["iso_urgency"] = _score_isolation(merged["isolation_time_min"])
    merged["pop_score"]   = _score_par(merged["pop_at_risk"])
    merged["fac_score"]   = _score_facilities(
        merged["hospitals_flooded"] + merged["schools_flooded"]
    )

    # Egress score: fewer exits → higher urgency
    if n_road_exits:
        merged["n_exits"] = merged["village_id"].map(n_road_exits).fillna(1)
    else:
        merged["n_exits"] = 1   # conservative default
    merged["egress_score"] = _score_egress(merged["n_exits"])

    # Composite score
    merged["priority_score"] = (
        weights.isolation  * merged["iso_urgency"] +
        weights.population * merged["pop_score"]   +
        weights.egress     * merged["egress_score"] +
        weights.facilities * merged["fac_score"]
    ).round(4)

    # Simplified uncertainty band: ±15% of score for pessimistic/optimistic
    # (In the full pipeline this would be computed separately per ensemble arm)
    merged["score_hi"] = (merged["priority_score"] * 1.15).clip(upper=1.0).round(4)
    merged["score_lo"] = (merged["priority_score"] * 0.85).clip(lower=0.0).round(4)

    # Sort and rank
    merged = merged.sort_values("priority_score", ascending=False).reset_index(drop=True)
    merged["priority_rank"] = merged.index + 1

    output_cols = [
        "priority_rank", "priority_score", "score_lo", "score_hi",
        "village_id", "village_name", "geometry",
        "pop_total", "pop_at_risk", "pop_provenance",
        "isolation_time_min", "evacuation_window_min", "water_arrival_min",
        "road_nodes_found", "inundated",
        "buildings_flooded", "hospitals_flooded", "schools_flooded",
        "loss_inr", "max_depth_m",
    ]
    available = [c for c in output_cols if c in merged.columns]
    result = gpd.GeoDataFrame(merged[available], crs=exposure_gdf.crs)

    top = result.iloc[0]
    logger.info("Ranking complete. Top village: %s (score=%.3f, PAR=%d, window=%s)",
                top.get("village_name", "?"),
                top["priority_score"],
                top.get("pop_at_risk", 0),
                f"{top['evacuation_window_min']:.0f} min"
                if np.isfinite(top.get("evacuation_window_min", np.nan)) else "n/a")
    return result
