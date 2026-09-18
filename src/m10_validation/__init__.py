"""
M10 — Validation against observed outcomes
==========================================
Scores a FloodSight run against real, independently-observed flood data.

Everything under this package is an observation made by someone else. It is
never relabelled as a model output, and a run with no observed counterpart
reports that fact rather than a score.
"""

from .metrics import (ExtentSkill, confusion, agreement_map,
                      AGREE_NONE, AGREE_HIT, AGREE_MISS, AGREE_FALSE,
                      AGREE_LABELS)
from .observed import (SOURCES, ObservedSource, ExtentComparison,
                       available, describe, compare_extent,
                       agreement_geojson, observed_extent_geojson,
                       load_observed_extent, load_observed_roads,
                       load_observed_domain)
from .roads import RoadSkill, compare_roads, DAMAGED_GRADES
from .compare_arrivals import compare_arrivals, HISTORICAL_ARRIVALS

__all__ = [
    "ExtentSkill", "confusion", "agreement_map",
    "AGREE_NONE", "AGREE_HIT", "AGREE_MISS", "AGREE_FALSE", "AGREE_LABELS",
    "SOURCES", "ObservedSource", "ExtentComparison",
    "available", "describe", "compare_extent",
    "agreement_geojson", "observed_extent_geojson",
    "load_observed_extent", "load_observed_roads", "load_observed_domain",
    "RoadSkill", "compare_roads", "DAMAGED_GRADES",
    "compare_arrivals", "HISTORICAL_ARRIVALS",
]
