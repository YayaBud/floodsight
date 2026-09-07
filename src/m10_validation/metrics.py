"""
M10 — Binary extent skill metrics
=================================
Scores a simulated flood extent against an observed one. This is the module
that turns "our model ran" into "our model was right, by this much".

Definitions (the standard hydrological set)
-------------------------------------------
With ``sim`` and ``obs`` as boolean masks over the *same* cells:

    TP = sim & obs        both say wet
    FP = sim & ~obs       we said wet, reality said dry   (over-prediction)
    FN = ~sim & obs       reality said wet, we missed it  (under-prediction)
    TN = ~sim & ~obs      both say dry

    CSI  = TP / (TP + FP + FN)      Critical Success Index — the headline
    POD  = TP / (TP + FN)           Probability of Detection
    FAR  = FP / (TP + FP)           False Alarm Ratio
    F1   = 2TP / (2TP + FP + FN)
    Bias = (TP + FP) / (TP + FN)    >1 over-predicts area, <1 under-predicts

TN is deliberately excluded from CSI. Flood extents are sparse: a domain that
is 99% dry scores 0.99 "accuracy" by predicting nothing at all. CSI is the
metric that cannot be gamed that way, which is why it is the one reported.

Why a domain mask is mandatory
------------------------------
Observed extents come from a mapped Area of Interest, not from the whole world.
Outside that AOI the observation is *absent*, not *dry*. Scoring there turns
every unmapped cell into a false alarm or a true negative and produces a number
that means nothing. ``confusion`` therefore requires the caller to say which
cells were actually observed.

Undefined is undefined
----------------------
Every ratio here returns ``None`` when its denominator is zero, never 0.0.
An empty observation and a perfect miss are different states, and collapsing
them to a number is the kind of well-meant fallback this project removed
everywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np


def _ratio(num: float, den: float) -> Optional[float]:
    """A ratio, or None when it is not defined. Never a silent zero."""
    return float(num) / float(den) if den > 0 else None


@dataclass
class ExtentSkill:
    """Confusion counts and the skill scores derived from them."""

    tp: int
    fp: int
    fn: int
    tn: int
    csi:  Optional[float]
    pod:  Optional[float]
    far:  Optional[float]
    f1:   Optional[float]
    bias: Optional[float]
    n_scored: int
    sim_area_cells: int
    obs_area_cells: int

    def as_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        def f(v):
            return "n/a" if v is None else f"{v:.3f}"
        return (f"CSI={f(self.csi)} POD={f(self.pod)} FAR={f(self.far)} "
                f"bias={f(self.bias)} (TP={self.tp} FP={self.fp} FN={self.fn}, "
                f"{self.n_scored} cells scored)")


def confusion(
    sim: np.ndarray,
    obs: np.ndarray,
    domain: np.ndarray,
) -> ExtentSkill:
    """
    Score ``sim`` against ``obs`` over the cells where ``domain`` is True.

    Parameters
    ----------
    sim    : boolean array — simulated wet.
    obs    : boolean array — observed wet.
    domain : boolean array — cells where an observation actually exists.
             Cells outside it are excluded entirely: unobserved is not dry.

    All three must be the same shape.
    """
    sim = np.asarray(sim, dtype=bool)
    obs = np.asarray(obs, dtype=bool)
    domain = np.asarray(domain, dtype=bool)
    if not (sim.shape == obs.shape == domain.shape):
        raise ValueError(
            f"shape mismatch: sim {sim.shape}, obs {obs.shape}, "
            f"domain {domain.shape} — align the grids before scoring"
        )

    s = sim & domain
    o = obs & domain

    tp = int(np.count_nonzero(s & o))
    fp = int(np.count_nonzero(s & ~o))
    fn = int(np.count_nonzero(~s & o))
    tn = int(np.count_nonzero(~s & ~o & domain))

    return ExtentSkill(
        tp=tp, fp=fp, fn=fn, tn=tn,
        csi=_ratio(tp, tp + fp + fn),
        pod=_ratio(tp, tp + fn),
        far=_ratio(fp, tp + fp),
        f1=_ratio(2 * tp, 2 * tp + fp + fn),
        bias=_ratio(tp + fp, tp + fn),
        n_scored=int(np.count_nonzero(domain)),
        sim_area_cells=tp + fp,
        obs_area_cells=tp + fn,
    )


#: Agreement raster class codes. Shared with the frontend legend, so the numbers
#: are part of the contract — do not renumber them casually.
AGREE_NONE = 0   # dry in both, or outside the observed domain
AGREE_HIT  = 1   # simulated and observed  — we got it right
AGREE_MISS = 2   # observed only           — the model missed real flooding
AGREE_FALSE = 3  # simulated only          — the model over-predicted

AGREE_LABELS = {
    AGREE_HIT:   "Hit — simulated and observed",
    AGREE_MISS:  "Miss — observed, not simulated",
    AGREE_FALSE: "False alarm — simulated, not observed",
}


def agreement_map(
    sim: np.ndarray,
    obs: np.ndarray,
    domain: np.ndarray,
) -> np.ndarray:
    """
    Per-cell agreement classes for the map overlay.

    Returns a uint8 array of ``AGREE_*`` codes. Cells outside ``domain`` are
    ``AGREE_NONE`` so the overlay stops at the edge of what was observed
    rather than implying a verdict where there is no observation.
    """
    sim = np.asarray(sim, dtype=bool)
    obs = np.asarray(obs, dtype=bool)
    domain = np.asarray(domain, dtype=bool)

    out = np.zeros(sim.shape, dtype=np.uint8)
    s, o = sim & domain, obs & domain
    out[s & o] = AGREE_HIT
    out[~s & o] = AGREE_MISS
    out[s & ~o] = AGREE_FALSE
    return out


def demo() -> None:
    """Self-check with hand-computable cases. Run: python -m src.m10_validation.metrics"""
    # ── A 4x4 grid with a known answer ───────────────────────────────────────
    # obs wet = 6 cells, sim wet = 6 cells, overlap = 4
    obs = np.zeros((4, 4), bool); obs[1:3, 0:3] = True          # 6 cells
    sim = np.zeros((4, 4), bool); sim[1:3, 1:4] = True          # 6 cells
    dom = np.ones((4, 4), bool)

    r = confusion(sim, obs, dom)
    assert (r.tp, r.fp, r.fn, r.tn) == (4, 2, 2, 8), (r.tp, r.fp, r.fn, r.tn)
    assert abs(r.csi - 4 / 8) < 1e-12          # 4 / (4+2+2)
    assert abs(r.pod - 4 / 6) < 1e-12
    assert abs(r.far - 2 / 6) < 1e-12
    assert abs(r.bias - 6 / 6) < 1e-12         # equal areas -> unbiased
    assert abs(r.f1 - 8 / 12) < 1e-12

    # ── Perfect agreement ────────────────────────────────────────────────────
    p = confusion(obs, obs, dom)
    assert p.csi == 1.0 and p.pod == 1.0 and p.far == 0.0 and p.bias == 1.0

    # ── Predicting nothing: CSI 0, FAR undefined (no positives at all) ───────
    z = confusion(np.zeros_like(obs), obs, dom)
    assert z.csi == 0.0 and z.pod == 0.0 and z.bias == 0.0
    assert z.far is None, "FAR with zero predicted-wet cells must be None, not 0"

    # ── Nothing observed: POD and bias undefined, not zero ───────────────────
    e = confusion(sim, np.zeros_like(obs), dom)
    assert e.pod is None and e.bias is None and e.far == 1.0

    # ── The domain mask must exclude, not treat unobserved as dry ────────────
    half = np.zeros((4, 4), bool); half[:, :2] = True
    m = confusion(sim, obs, half)
    # In the left half: obs cols 0-1 rows 1-2 = 4 wet; sim col 1 rows 1-2 = 2 wet
    assert (m.tp, m.fp, m.fn) == (2, 0, 2), (m.tp, m.fp, m.fn)
    assert m.n_scored == 8
    # ...and the excluded half must not have leaked in as false alarms
    assert m.fp == 0

    # ── Agreement map ────────────────────────────────────────────────────────
    a = agreement_map(sim, obs, dom)
    assert int(np.count_nonzero(a == AGREE_HIT)) == 4
    assert int(np.count_nonzero(a == AGREE_MISS)) == 2
    assert int(np.count_nonzero(a == AGREE_FALSE)) == 2
    a2 = agreement_map(sim, obs, half)
    assert np.all(a2[:, 2:] == AGREE_NONE), "verdict rendered outside the observed domain"

    # ── Shape mismatch is an error, not a silent broadcast ───────────────────
    try:
        confusion(np.zeros((2, 2), bool), obs, dom)
    except ValueError:
        pass
    else:
        raise AssertionError("mismatched shapes must raise")

    print("m10_validation.metrics: all checks passed")


if __name__ == "__main__":
    demo()
