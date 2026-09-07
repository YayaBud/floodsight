"""
M10 — Road impact validation
============================
Scores the simulation's road-cut prediction against **observed** road damage.

M6 is this project's claimed differentiator: it cuts a road graph as a function
of simulation time. Until now that prediction had never been compared to
anything real. Copernicus EMS activation EMSR696 graded every road link in
Derna after the two dams failed, which makes the comparison possible.

Why sample the observed geometry instead of matching two networks
-----------------------------------------------------------------
Our links come from OSM via OSMnx; the observed links were digitised by a
Copernicus operator from post-event imagery. They are different geometries from
different sources and will not share identifiers, node ids, or even splits. A
network-to-network match would need a conflation step whose own error would
dominate the result.

So the observed geometry is used as the *sampling location*: for each observed
link, ask our depth raster how deep the water got along that link. The test
becomes "where Copernicus mapped a road, did our model put enough water on it
to match what happened to it?" — which is the question that matters and needs
no conflation.

Grades
------
Copernicus grades are ``Destroyed``, ``Damaged``, ``Possibly damaged``.
``Possibly damaged`` means the operator could not confirm from imagery. It is
excluded from the confusion matrix by default and reported separately rather
than silently folded into either class — guessing on the analyst's behalf is
how an honest uncertain becomes a dishonest certain.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
import geopandas as gpd
import rasterio

from ..rasterutils import sample_raster
from .metrics import _ratio
from .observed import load_observed_roads, SOURCES

logger = logging.getLogger(__name__)

#: Grades treated as "the road was actually lost".
DAMAGED_GRADES = ("Destroyed", "Damaged")
#: Reported, but never scored as either class.
UNCERTAIN_GRADES = ("Possibly damaged",)


def _sample_along(geom, depth: np.ndarray, transform, n: int = 9) -> float:
    """
    Maximum simulated depth along a link.

    A midpoint alone is a poor proxy: a 400 m link crossing a wadi is cut at the
    crossing, not at its middle. ``n`` evenly-spaced samples are taken and the
    maximum is used, matching how a road actually fails — at its worst point.
    """
    if geom is None or geom.is_empty:
        return 0.0
    parts = geom.geoms if geom.geom_type == "MultiLineString" else [geom]
    best = 0.0
    for part in parts:
        fracs = np.linspace(0.0, 1.0, max(2, n))
        pts = [part.interpolate(f, normalized=True) for f in fracs]
        xs = np.array([p.x for p in pts])
        ys = np.array([p.y for p in pts])
        d = sample_raster(depth, transform, xs, ys)
        if d.size:
            best = max(best, float(np.nanmax(d)))
    return best


@dataclass
class RoadSkill:
    """Confusion over observed road links, plus the uncertain ones."""
    tp: int
    fp: int
    fn: int
    tn: int
    pod: Optional[float]
    far: Optional[float]
    csi: Optional[float]
    accuracy: Optional[float]
    n_scored: int
    n_uncertain_excluded: int
    n_observed_total: int
    threshold_m: float
    grade_counts: dict
    #: False when the observed dataset contains no undamaged links, i.e. there
    #: are no true negatives to get right. Copernicus digitises damaged assets,
    #: so every scored link is damaged and FAR is structurally 0 no matter what
    #: the model does. A 0.000 that cannot be anything else is not a result,
    #: and the UI must not present it as one.
    far_meaningful: bool = True

    def as_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        def f(v):
            return "n/a" if v is None else f"{v:.3f}"
        far = f(self.far) if self.far_meaningful else "n/a (no undamaged links observed)"
        return (f"roads: POD={f(self.pod)} FAR={far} CSI={f(self.csi)} "
                f"(TP={self.tp} FP={self.fp} FN={self.fn} TN={self.tn}; "
                f"{self.n_uncertain_excluded} uncertain excluded)")


def compare_roads(
    max_depth_raster: str | Path,
    scenario_key: str,
    threshold_m: float = 0.30,
    damaged_grades: Sequence[str] = DAMAGED_GRADES,
    include_uncertain: bool = False,
) -> tuple[RoadSkill, gpd.GeoDataFrame]:
    """
    Score simulated road inundation against observed damage grades.

    Returns ``(skill, gdf)`` where ``gdf`` carries the observed geometry plus
    ``sim_depth_m``, ``sim_cut``, ``obs_damaged`` and ``outcome`` per link, so
    the map can render exactly which links the model got right and wrong.
    """
    src = SOURCES.get(scenario_key)
    if src is None or not src.roads_glob:
        raise FileNotFoundError(f"no observed roads wired for {scenario_key}")

    roads = load_observed_roads(scenario_key)
    if "damage_gra" not in roads.columns:
        raise ValueError("observed roads carry no 'damage_gra' column")

    with rasterio.open(max_depth_raster) as ds:
        depth = np.nan_to_num(ds.read(1).astype(float), nan=0.0)
        transform, crs = ds.transform, ds.crs

    roads_proj = roads.to_crs(crs)
    sim_depth = np.array([_sample_along(g, depth, transform)
                          for g in roads_proj.geometry])

    grade = roads["damage_gra"].astype(str)
    uncertain = grade.isin(UNCERTAIN_GRADES).to_numpy()
    obs_damaged = grade.isin(damaged_grades).to_numpy()
    sim_cut = sim_depth >= threshold_m

    scored = np.ones(len(roads), dtype=bool) if include_uncertain else ~uncertain

    tp = int(np.count_nonzero(scored & sim_cut & obs_damaged))
    fp = int(np.count_nonzero(scored & sim_cut & ~obs_damaged))
    fn = int(np.count_nonzero(scored & ~sim_cut & obs_damaged))
    tn = int(np.count_nonzero(scored & ~sim_cut & ~obs_damaged))

    outcome = np.full(len(roads), "excluded", dtype=object)
    outcome[scored & sim_cut & obs_damaged] = "hit"
    outcome[scored & sim_cut & ~obs_damaged] = "false_alarm"
    outcome[scored & ~sim_cut & obs_damaged] = "miss"
    outcome[scored & ~sim_cut & ~obs_damaged] = "correct_negative"

    out = roads.copy()
    out["sim_depth_m"] = np.round(sim_depth, 3)
    out["sim_cut"] = sim_cut
    out["obs_damaged"] = obs_damaged
    out["outcome"] = outcome

    skill = RoadSkill(
        tp=tp, fp=fp, fn=fn, tn=tn,
        pod=_ratio(tp, tp + fn),
        far=_ratio(fp, tp + fp),
        csi=_ratio(tp, tp + fp + fn),
        accuracy=_ratio(tp + tn, tp + fp + fn + tn),
        n_scored=int(np.count_nonzero(scored)),
        n_uncertain_excluded=int(np.count_nonzero(uncertain)) if not include_uncertain else 0,
        n_observed_total=len(roads),
        threshold_m=threshold_m,
        grade_counts={str(k): int(v) for k, v in grade.value_counts().items()},
        far_meaningful=bool((fp + tn) > 0),
    )
    logger.info("M10: %s", skill.summary())
    return skill, out


def demo() -> None:
    """Self-check on a synthetic raster and three hand-placed links."""
    from rasterio.transform import from_origin
    from shapely.geometry import LineString

    tr = from_origin(0.0, 100.0, 10.0, 10.0)
    depth = np.zeros((10, 10))
    depth[0, :] = 2.0                       # top row is deeply flooded

    # A link along the top row (wet), one along the bottom (dry), and one that
    # only clips the wet row at its far end — the case a midpoint sample misses.
    top = LineString([(5, 95), (95, 95)])
    bot = LineString([(5, 5), (95, 5)])
    clip = LineString([(5, 55), (5, 95)])

    assert _sample_along(top, depth, tr) == 2.0
    assert _sample_along(bot, depth, tr) == 0.0
    assert _sample_along(clip, depth, tr) == 2.0, \
        "an end-of-link crossing must be found; midpoint-only sampling misses it"

    # Uncertain grades must be excluded, not folded into a class.
    g = ["Destroyed", "Possibly damaged", "Damaged"]
    import pandas as pd
    s = pd.Series(g)
    assert int(s.isin(UNCERTAIN_GRADES).sum()) == 1
    assert int(s.isin(DAMAGED_GRADES).sum()) == 2

    print("m10_validation.roads: all checks passed")


if __name__ == "__main__":
    demo()
