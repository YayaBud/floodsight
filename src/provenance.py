"""
FloodSight — Provenance Labels
==============================
One vocabulary for "where did this number come from", shared by the pipeline,
the API and the dashboard.

The README declares integrity rules. This module is how they are enforced in
code rather than asserted in prose: every value that reaches the screen carries
one of these labels, and the label is set at the point the value is produced —
not chosen later by the UI.

Rules
-----
- ``COMPUTED_LIVE``     the number came out of a solver or an analysis module
                        in this run.
- ``PRECOMPUTED``       computed by us, offline, before the session. Legitimate
                        (a scenario library is the correct architecture) but it
                        must never be presented as live.
- ``PROXY_DATA``        a real measurement stands in for the one we want, e.g.
                        OSM schools as a shelter proxy, or a declared village
                        population where no gridded population raster is loaded.
- ``SYNTHETIC_TERRAIN`` the DEM is analytic, not surveyed. Demo-only.
- ``NOT_AVAILABLE``     we could not compute it. Render as a dash, never as 0
                        and never as an estimate.

``NOT_AVAILABLE`` exists so that a missing value has somewhere honest to go.
Most fabrication starts as a well-meant fallback.
"""

from __future__ import annotations

from enum import Enum


class Provenance(str, Enum):
    COMPUTED_LIVE     = "COMPUTED_LIVE"
    PRECOMPUTED       = "PRECOMPUTED"
    PROXY_DATA        = "PROXY_DATA"
    SYNTHETIC_TERRAIN = "SYNTHETIC_TERRAIN"
    NOT_AVAILABLE     = "NOT_AVAILABLE"

    def __str__(self) -> str:            # so json.dump and f-strings stay clean
        return self.value


#: Human-readable text for the dashboard badge.
LABELS: dict[str, str] = {
    Provenance.COMPUTED_LIVE.value:     "COMPUTED LIVE",
    Provenance.PRECOMPUTED.value:       "PRECOMPUTED",
    Provenance.PROXY_DATA.value:        "PROXY DATA",
    Provenance.SYNTHETIC_TERRAIN.value: "SYNTHETIC TERRAIN",
    Provenance.NOT_AVAILABLE.value:     "NOT AVAILABLE",
}

#: One-line explanation shown on hover. Says what the label actually means for
#: the number next to it, in the analyst's terms.
TOOLTIPS: dict[str, str] = {
    Provenance.COMPUTED_LIVE.value:
        "Produced by a solver or analysis module during this run.",
    Provenance.PRECOMPUTED.value:
        "Computed by this system offline, before the session. Not a live result.",
    Provenance.PROXY_DATA.value:
        "A stand-in dataset. The real measurement was not available.",
    Provenance.SYNTHETIC_TERRAIN.value:
        "Analytic terrain, not a surveyed DEM. Depths and timings are "
        "illustrative and must not be used operationally.",
    Provenance.NOT_AVAILABLE.value:
        "Could not be computed from the available data.",
}


def worst(*labels: "Provenance | str | None") -> Provenance:
    """
    Combine provenance labels for a derived value, returning the weakest one.

    A number computed from a synthetic DEM is synthetic no matter how good the
    solver is, so a derived value inherits the weakest provenance of its inputs.

    ``None`` inputs are ignored, which lets callers pass optional stages without
    branching. With nothing to combine the result is ``NOT_AVAILABLE``.
    """
    order = [
        Provenance.COMPUTED_LIVE,
        Provenance.PRECOMPUTED,
        Provenance.PROXY_DATA,
        Provenance.SYNTHETIC_TERRAIN,
        Provenance.NOT_AVAILABLE,
    ]
    rank = {p: i for i, p in enumerate(order)}

    present = [Provenance(l) for l in labels if l is not None]
    if not present:
        return Provenance.NOT_AVAILABLE
    return max(present, key=lambda p: rank[p])


def demo() -> None:
    """Self-check: the combination rule is what protects every derived number."""
    assert worst(Provenance.COMPUTED_LIVE) is Provenance.COMPUTED_LIVE
    # a live solver on synthetic terrain is still synthetic
    assert worst(Provenance.COMPUTED_LIVE,
                 Provenance.SYNTHETIC_TERRAIN) is Provenance.SYNTHETIC_TERRAIN
    # a proxy population beats nothing but loses to a live computation
    assert worst(Provenance.COMPUTED_LIVE,
                 Provenance.PROXY_DATA) is Provenance.PROXY_DATA
    # anything missing dominates everything
    assert worst(Provenance.COMPUTED_LIVE,
                 Provenance.NOT_AVAILABLE) is Provenance.NOT_AVAILABLE
    # strings are accepted, None is ignored, empty means NOT_AVAILABLE
    assert worst("COMPUTED_LIVE", None) is Provenance.COMPUTED_LIVE
    assert worst() is Provenance.NOT_AVAILABLE
    # str() stays clean for json / f-strings
    assert f"{Provenance.PROXY_DATA}" == "PROXY_DATA"
    assert set(LABELS) == {p.value for p in Provenance}
    assert set(TOOLTIPS) == {p.value for p in Provenance}
    print("provenance: all checks passed")


if __name__ == "__main__":
    demo()
